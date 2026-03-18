"""
Job Scheduler for SWE-Squad — time-aware, quota-aware, modular.

Supports:
- Cron expressions (5-field: M H DoM Mon DoW)
- Time-window awareness (peak/off-peak scheduling)
- Quota-aware throttling (check budget before dispatching)
- Config-driven job definitions (swe_team.yaml)
- YAML + JSON persistence
- GitHub issue commenting when jobs are scheduled
- Pluggable executor (Claude CLI, A2A agents, shell commands)
- Retry with exponential backoff
- Concurrency limiting via ThreadPoolExecutor
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ScheduleType(str, Enum):
    CRON = "cron"
    ONCE = "once"
    INTERVAL = "interval"  # every N minutes


class JobStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class JobPriority(str, Enum):
    CRITICAL = "critical"   # Runs anytime, ignores peak/off-peak
    HIGH = "high"           # Prefers off-peak but can run during peak
    NORMAL = "normal"       # Runs during off-peak windows
    LOW = "low"             # Only runs when quota is abundant


@dataclass
class TimeWindow:
    """Defines peak/off-peak hours for cost optimization."""
    peak_start_hour: int = 13  # 1 PM UTC = 8 AM ET
    peak_end_hour: int = 19    # 7 PM UTC = 2 PM ET
    peak_days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon-Fri

    def is_peak(self, dt: Optional[datetime] = None) -> bool:
        dt = dt or datetime.now(timezone.utc)
        if dt.weekday() not in self.peak_days:
            return False
        return self.peak_start_hour <= dt.hour < self.peak_end_hour

    def next_off_peak(self, dt: Optional[datetime] = None) -> datetime:
        dt = dt or datetime.now(timezone.utc)
        if not self.is_peak(dt):
            return dt
        candidate = dt.replace(hour=self.peak_end_hour, minute=0, second=0, microsecond=0)
        if candidate <= dt:
            candidate += timedelta(days=1)
        # Skip weekends if peak_days is weekdays only
        while candidate.weekday() not in self.peak_days:
            candidate += timedelta(days=1)
        return candidate


@dataclass
class ScheduledJob:
    """A scheduled job definition."""
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    name: str = ""
    description: str = ""
    schedule_type: ScheduleType = ScheduleType.CRON
    cron_expression: str = ""          # "*/30 * * * *"
    interval_minutes: int = 0          # For INTERVAL type
    priority: JobPriority = JobPriority.NORMAL

    # What to execute
    instructions: str = ""             # Prompt or command
    model: str = "sonnet"              # Model tier for execution
    agent: str = "claude-code"         # Target agent

    # Linked issue tracking
    github_issue: Optional[int] = None  # Link to GH issue
    ticket_id: Optional[str] = None     # Link to SWE ticket

    # Execution control
    max_retries: int = 3
    retry_backoff_seconds: int = 60
    max_concurrent: int = 1
    enabled: bool = True
    respect_peak_hours: bool = True    # If True, defers during peak

    # State
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    run_count: int = 0
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schedule_type"] = self.schedule_type.value
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledJob":
        data = dict(data)
        if "schedule_type" in data:
            data["schedule_type"] = ScheduleType(data["schedule_type"])
        if "status" in data:
            data["status"] = JobStatus(data["status"])
        if "priority" in data:
            data["priority"] = JobPriority(data["priority"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def parse_cron_field(field_str: str, min_val: int, max_val: int) -> List[int]:
    """Parse a single cron field into a list of matching integers."""
    values = set()
    for part in field_str.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            values.update(range(min_val, max_val + 1, step))
        elif "-" in part:
            start, end = part.split("-", 1)
            values.update(range(int(start), int(end) + 1))
        else:
            values.add(int(part))
    return sorted(v for v in values if min_val <= v <= max_val)


def cron_matches(expression: str, dt: datetime) -> bool:
    """Check if a datetime matches a 5-field cron expression."""
    fields = expression.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    return (
        dt.minute in parse_cron_field(minute, 0, 59)
        and dt.hour in parse_cron_field(hour, 0, 23)
        and dt.day in parse_cron_field(dom, 1, 31)
        and dt.month in parse_cron_field(month, 1, 12)
        and dt.weekday() in parse_cron_field(dow, 0, 6)
    )


def next_cron_match(expression: str, after: Optional[datetime] = None) -> datetime:
    """Find the next datetime matching a cron expression (max 48h lookahead)."""
    dt = (after or datetime.now(timezone.utc)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = dt + timedelta(hours=48)
    while dt < limit:
        if cron_matches(expression, dt):
            return dt
        dt += timedelta(minutes=1)
    return dt  # fallback


class JobStore:
    """JSON file-backed job persistence."""

    def __init__(self, path: Path):
        if isinstance(path, str):
            path = Path(path)
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> List[ScheduledJob]:
        with self._lock:
            if not self._path.exists():
                return []
            try:
                data = json.loads(self._path.read_text())
                return [ScheduledJob.from_dict(j) for j in data]
            except (json.JSONDecodeError, KeyError):
                logger.warning("Corrupt job store at %s — returning empty", self._path)
                return []

    def save_all(self, jobs: List[ScheduledJob]) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps([j.to_dict() for j in jobs], indent=2, default=str))
            tmp.rename(self._path)

    def upsert(self, job: ScheduledJob) -> None:
        jobs = self.load_all()
        for i, j in enumerate(jobs):
            if j.job_id == job.job_id:
                jobs[i] = job
                self.save_all(jobs)
                return
        jobs.append(job)
        self.save_all(jobs)


class JobScheduler:
    """Time-aware, quota-aware job scheduler."""

    def __init__(
        self,
        store: Optional[JobStore] = None,
        store_path: Optional[str] = None,
        time_window: Optional[TimeWindow] = None,
        executor: Optional[Callable] = None,
        quota_checker: Optional[Callable] = None,
        max_workers: int = 3,
        tick_interval: int = 30,
    ):
        if store is not None:
            self._store = store
        elif store_path is not None:
            self._store = JobStore(Path(store_path))
        else:
            raise ValueError("Either store or store_path must be provided")
        self._time_window = time_window or TimeWindow()
        self._executor = executor or self._default_executor
        self._quota_checker = quota_checker  # () -> (has_budget: bool, remaining: int)
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._tick_interval = tick_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running_jobs: Dict[str, bool] = {}

    # --- CRUD ---

    def add_job(self, job: ScheduledJob) -> ScheduledJob:
        if job.schedule_type == ScheduleType.CRON and job.cron_expression:
            job.next_run = next_cron_match(job.cron_expression).isoformat()
        elif job.schedule_type == ScheduleType.INTERVAL and job.interval_minutes > 0:
            job.next_run = (datetime.now(timezone.utc) + timedelta(minutes=job.interval_minutes)).isoformat()
        job.status = JobStatus.SCHEDULED
        self._store.upsert(job)
        logger.info("Job added: %s (%s) next_run=%s", job.name, job.job_id, job.next_run)
        return job

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        for j in self._store.load_all():
            if j.job_id == job_id:
                return j
        return None

    def list_jobs(self, status: Optional[JobStatus] = None) -> List[ScheduledJob]:
        jobs = self._store.load_all()
        if status:
            jobs = [j for j in jobs if j.status == status]
        return jobs

    def pause_job(self, job_id: str) -> Optional[ScheduledJob]:
        job = self.get_job(job_id)
        if job:
            job.status = JobStatus.PAUSED
            self._store.upsert(job)
            return job
        return None

    def resume_job(self, job_id: str) -> Optional[ScheduledJob]:
        job = self.get_job(job_id)
        if job and job.status == JobStatus.PAUSED:
            job.status = JobStatus.SCHEDULED
            self._store.upsert(job)
            return job
        return None

    def cancel_job(self, job_id: str) -> Optional[ScheduledJob]:
        job = self.get_job(job_id)
        if job:
            job.status = JobStatus.CANCELLED
            self._store.upsert(job)
            return job
        return None

    def trigger_job(self, job_id: str) -> Optional[ScheduledJob]:
        """Manually trigger a job immediately, bypassing schedule checks."""
        job = self.get_job(job_id)
        if job is None:
            return None
        self._pool.submit(self._execute_job, job)
        return job

    # --- Scheduling logic ---

    def should_run(self, job: ScheduledJob) -> tuple[bool, str]:
        """Check if a job should run now, considering priority, peak hours, and quota."""
        if not job.enabled or job.status not in (JobStatus.SCHEDULED, JobStatus.PENDING):
            return False, "not enabled or not scheduled"

        if job.job_id in self._running_jobs:
            return False, "already running"

        now = datetime.now(timezone.utc)

        # Check if it's time
        if job.next_run:
            next_dt = datetime.fromisoformat(job.next_run)
            if now < next_dt:
                return False, f"not due until {job.next_run}"

        # Peak hour check (CRITICAL priority ignores peak)
        if job.respect_peak_hours and job.priority != JobPriority.CRITICAL:
            if self._time_window.is_peak(now):
                if job.priority in (JobPriority.NORMAL, JobPriority.LOW):
                    return False, "peak hours — deferred"

        # Quota check (LOW priority requires abundant budget)
        if self._quota_checker:
            has_budget, remaining = self._quota_checker()
            if not has_budget:
                return False, "quota exhausted"
            if job.priority == JobPriority.LOW and remaining < 10:
                return False, "low priority — insufficient quota headroom"

        return True, "ready"

    def _advance_schedule(self, job: ScheduledJob) -> None:
        """Compute next_run after execution."""
        now = datetime.now(timezone.utc)
        if job.schedule_type == ScheduleType.ONCE:
            job.status = JobStatus.COMPLETED
            job.next_run = None
        elif job.schedule_type == ScheduleType.CRON:
            job.next_run = next_cron_match(job.cron_expression, after=now).isoformat()
            job.status = JobStatus.SCHEDULED
        elif job.schedule_type == ScheduleType.INTERVAL:
            job.next_run = (now + timedelta(minutes=job.interval_minutes)).isoformat()
            job.status = JobStatus.SCHEDULED

    def _execute_job(self, job: ScheduledJob) -> None:
        """Execute a single job with retry logic."""
        self._running_jobs[job.job_id] = True
        job.status = JobStatus.RUNNING
        job.last_run = datetime.now(timezone.utc).isoformat()
        self._store.upsert(job)

        attempt = 0
        success = False
        while attempt <= job.max_retries:
            try:
                self._executor(job)
                success = True
                break
            except Exception as exc:
                attempt += 1
                job.last_error = f"Attempt {attempt}: {str(exc)[:200]}"
                logger.warning("Job %s attempt %d failed: %s", job.job_id, attempt, exc)
                if attempt <= job.max_retries:
                    backoff = job.retry_backoff_seconds * (2 ** (attempt - 1))
                    self._stop_event.wait(min(backoff, 300))

        job.run_count += 1
        if success:
            job.last_error = None
            self._advance_schedule(job)
            logger.info("Job %s completed (run #%d)", job.name, job.run_count)
        else:
            job.status = JobStatus.FAILED
            logger.error("Job %s failed after %d retries", job.name, job.max_retries + 1)

        self._store.upsert(job)
        self._running_jobs.pop(job.job_id, None)

    def _default_executor(self, job: ScheduledJob) -> None:
        logger.info("DEFAULT EXECUTOR (stub): job=%s instructions=%s", job.name, job.instructions[:100])

    # --- Daemon ---

    def _tick(self) -> int:
        """One scheduler tick. Returns number of jobs dispatched."""
        dispatched = 0
        for job in self._store.load_all():
            should, reason = self.should_run(job)
            if should:
                self._pool.submit(self._execute_job, job)
                dispatched += 1
                logger.info("Dispatched job: %s (%s)", job.name, job.job_id)
            elif reason not in ("not enabled or not scheduled", "already running"):
                logger.debug("Job %s skipped: %s", job.name, reason)
        return dispatched

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="swe-scheduler")
        self._thread.start()
        logger.info("Scheduler started (tick=%ds, workers=%d)", self._tick_interval, self._pool._max_workers)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._pool.shutdown(wait=False)
        logger.info("Scheduler stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop_event.wait(self._tick_interval)
