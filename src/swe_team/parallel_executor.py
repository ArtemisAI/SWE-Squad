"""
Parallel Execution Engine for SWE-Squad.

Replaces sequential cycle processing with a pool-based parallel executor
that runs multiple investigations/developments concurrently using
ThreadPoolExecutor. Claude CLI calls are subprocess-based, so threads
work without GIL contention.

Usage::

    from src.swe_team.parallel_executor import ParallelExecutor

    executor = ParallelExecutor(config=execution_config)
    futures = [executor.submit_investigation(t, investigator) for t in tickets]
    results = executor.collect_results(futures)
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.swe_team.rate_limiter import RateLimitCooldown

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ExecutionProfile:
    """A named concurrency profile (base, burst, max)."""

    max_concurrent_investigations: int = 2
    max_concurrent_developments: int = 1
    cycle_interval_seconds: int = 900

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionProfile":
        return cls(
            max_concurrent_investigations=data.get("max_concurrent_investigations", 2),
            max_concurrent_developments=data.get("max_concurrent_developments", 1),
            cycle_interval_seconds=data.get("cycle_interval_seconds", 900),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_concurrent_investigations": self.max_concurrent_investigations,
            "max_concurrent_developments": self.max_concurrent_developments,
            "cycle_interval_seconds": self.cycle_interval_seconds,
        }


@dataclass
class AdaptiveScheduleEntry:
    """A time-based profile selection rule."""

    hours: str = "0-24"
    profile: str = "base"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdaptiveScheduleEntry":
        return cls(
            hours=data.get("hours", "0-24"),
            profile=data.get("profile", "base"),
        )


@dataclass
class AdaptiveConfig:
    """Settings for adaptive mode profile selection."""

    schedule: List[AdaptiveScheduleEntry] = field(default_factory=list)
    backlog_burst_threshold: int = 30
    backlog_max_threshold: int = 80
    quota_multiplier: float = 1.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdaptiveConfig":
        schedule = [
            AdaptiveScheduleEntry.from_dict(s)
            for s in data.get("schedule", [])
        ]
        return cls(
            schedule=schedule,
            backlog_burst_threshold=data.get("backlog_burst_threshold", 30),
            backlog_max_threshold=data.get("backlog_max_threshold", 80),
            quota_multiplier=data.get("quota_multiplier", 1.0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schedule": [{"hours": s.hours, "profile": s.profile} for s in self.schedule],
            "backlog_burst_threshold": self.backlog_burst_threshold,
            "backlog_max_threshold": self.backlog_max_threshold,
            "quota_multiplier": self.quota_multiplier,
        }


@dataclass
class ExecutionConfig:
    """Top-level execution configuration."""

    mode: str = "sequential"  # "sequential" | "parallel" | "adaptive"
    profiles: Dict[str, ExecutionProfile] = field(default_factory=lambda: {
        "base": ExecutionProfile(
            max_concurrent_investigations=2,
            max_concurrent_developments=1,
            cycle_interval_seconds=900,
        ),
        "burst": ExecutionProfile(
            max_concurrent_investigations=5,
            max_concurrent_developments=3,
            cycle_interval_seconds=300,
        ),
        "max": ExecutionProfile(
            max_concurrent_investigations=8,
            max_concurrent_developments=5,
            cycle_interval_seconds=120,
        ),
    })
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionConfig":
        profiles = {}
        for name, profile_data in data.get("profiles", {}).items():
            profiles[name] = ExecutionProfile.from_dict(profile_data)
        # Fill in defaults for missing profiles
        if "base" not in profiles:
            profiles["base"] = ExecutionProfile()
        if "burst" not in profiles:
            profiles["burst"] = ExecutionProfile(
                max_concurrent_investigations=5,
                max_concurrent_developments=3,
                cycle_interval_seconds=300,
            )
        if "max" not in profiles:
            profiles["max"] = ExecutionProfile(
                max_concurrent_investigations=8,
                max_concurrent_developments=5,
                cycle_interval_seconds=120,
            )

        adaptive = AdaptiveConfig.from_dict(data.get("adaptive", {}))
        return cls(
            mode=data.get("mode", "sequential"),
            profiles=profiles,
            adaptive=adaptive,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "profiles": {k: v.to_dict() for k, v in self.profiles.items()},
            "adaptive": self.adaptive.to_dict(),
        }


# ---------------------------------------------------------------------------
# Throughput metrics
# ---------------------------------------------------------------------------

@dataclass
class ThroughputMetrics:
    """Tracks throughput statistics for the parallel executor."""

    investigations_completed: int = 0
    investigations_failed: int = 0
    developments_completed: int = 0
    developments_failed: int = 0
    total_investigation_time_s: float = 0.0
    total_development_time_s: float = 0.0
    _start_time: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_investigation(self, duration_s: float, success: bool) -> None:
        with self._lock:
            if success:
                self.investigations_completed += 1
            else:
                self.investigations_failed += 1
            self.total_investigation_time_s += duration_s

    def record_development(self, duration_s: float, success: bool) -> None:
        with self._lock:
            if success:
                self.developments_completed += 1
            else:
                self.developments_failed += 1
            self.total_development_time_s += duration_s

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time metrics snapshot."""
        with self._lock:
            elapsed_h = max(0.001, (time.monotonic() - self._start_time) / 3600)
            total_inv = self.investigations_completed + self.investigations_failed
            total_dev = self.developments_completed + self.developments_failed
            return {
                "investigations_completed": self.investigations_completed,
                "investigations_failed": self.investigations_failed,
                "developments_completed": self.developments_completed,
                "developments_failed": self.developments_failed,
                "avg_investigation_time_s": round(
                    self.total_investigation_time_s / total_inv, 2
                ) if total_inv > 0 else 0.0,
                "avg_development_time_s": round(
                    self.total_development_time_s / total_dev, 2
                ) if total_dev > 0 else 0.0,
                "investigations_per_hour": round(total_inv / elapsed_h, 2),
                "developments_per_hour": round(total_dev / elapsed_h, 2),
                "total_tickets_per_hour": round((total_inv + total_dev) / elapsed_h, 2),
                "elapsed_hours": round(elapsed_h, 3),
            }


# ---------------------------------------------------------------------------
# Task result wrapper
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    """Result from a parallel task execution."""

    ticket_id: str
    task_type: str  # "investigation" | "development"
    success: bool
    duration_s: float
    error: Optional[str] = None
    ticket: Any = None  # The updated SWETicket object


# ---------------------------------------------------------------------------
# Parallel Executor
# ---------------------------------------------------------------------------

class ParallelExecutor:
    """Pool-based parallel executor for investigation and development tasks.

    Uses ThreadPoolExecutor since Claude CLI calls are subprocess-based
    and do not hold the GIL. Each worker operates in its own git worktree
    to avoid conflicts.

    Parameters
    ----------
    execution_config:
        ExecutionConfig loaded from swe_team.yaml execution section.
    active_profile:
        Name of the initial profile to use ("base", "burst", "max").
        Ignored in adaptive mode where profile is auto-selected.
    on_ticket_complete:
        Optional callback invoked after each ticket completes (for
        per-ticket persistence). Signature: (ticket, task_type, success) -> None
    """

    def __init__(
        self,
        execution_config: ExecutionConfig,
        active_profile: str = "base",
        on_ticket_complete: Optional[Callable] = None,
    ) -> None:
        self._config = execution_config
        self._active_profile_name = active_profile
        self._on_ticket_complete = on_ticket_complete
        self._metrics = ThroughputMetrics()

        self._investigation_pool: Optional[ThreadPoolExecutor] = None
        self._development_pool: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()
        self._active_investigations: int = 0
        self._active_developments: int = 0
        self._shutdown = False

        # Initialize pools based on current profile
        self._rebuild_pools()

    @property
    def active_profile_name(self) -> str:
        return self._active_profile_name

    @property
    def active_profile(self) -> ExecutionProfile:
        return self._config.profiles.get(
            self._active_profile_name,
            self._config.profiles.get("base", ExecutionProfile()),
        )

    @property
    def metrics(self) -> ThroughputMetrics:
        return self._metrics

    def _rebuild_pools(self) -> None:
        """(Re)create thread pools based on the active profile."""
        profile = self.active_profile
        # Shut down old pools gracefully
        if self._investigation_pool is not None:
            self._investigation_pool.shutdown(wait=False)
        if self._development_pool is not None:
            self._development_pool.shutdown(wait=False)

        self._investigation_pool = ThreadPoolExecutor(
            max_workers=profile.max_concurrent_investigations,
            thread_name_prefix="swe-investigate",
        )
        self._development_pool = ThreadPoolExecutor(
            max_workers=profile.max_concurrent_developments,
            thread_name_prefix="swe-develop",
        )
        logger.info(
            "Parallel executor pools created: investigations=%d, developments=%d (profile=%s)",
            profile.max_concurrent_investigations,
            profile.max_concurrent_developments,
            self._active_profile_name,
        )

    def scale_to(self, profile_name: str) -> None:
        """Dynamically switch to a different concurrency profile.

        Active tasks continue running; new submissions use the new pool sizes.
        """
        if profile_name not in self._config.profiles:
            raise ValueError(
                f"Unknown profile '{profile_name}'. "
                f"Available: {list(self._config.profiles.keys())}"
            )
        if profile_name == self._active_profile_name:
            return
        old = self._active_profile_name
        self._active_profile_name = profile_name
        self._rebuild_pools()
        logger.info("Parallel executor scaled from '%s' to '%s'", old, profile_name)

    def resolve_adaptive_profile(
        self,
        *,
        backlog_size: int = 0,
        now_utc: Optional[datetime] = None,
    ) -> str:
        """Determine which profile to use in adaptive mode.

        Checks backlog thresholds first (override), then time-based schedule.
        """
        adaptive = self._config.adaptive

        # Backlog overrides take priority
        if backlog_size >= adaptive.backlog_max_threshold:
            return "max"
        if backlog_size >= adaptive.backlog_burst_threshold:
            return "burst"

        # Time-based schedule
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour

        for entry in adaptive.schedule:
            start_s, end_s = entry.hours.split("-")
            start_h, end_h = int(start_s), int(end_s)
            if start_h <= hour < end_h:
                if entry.profile in self._config.profiles:
                    return entry.profile

        return "base"

    def submit_investigation(
        self,
        ticket: Any,
        investigator: Any,
        *,
        worktree_path: Optional[str] = None,
    ) -> Future:
        """Submit a ticket for parallel investigation.

        Parameters
        ----------
        ticket:
            SWETicket to investigate.
        investigator:
            InvestigatorAgent instance (must have .investigate(ticket) method).
        worktree_path:
            Optional git worktree path for this worker.

        Returns
        -------
        Future that resolves to a TaskResult.
        """
        if self._shutdown:
            raise RuntimeError("Executor has been shut down")

        def _run() -> TaskResult:
            start = time.monotonic()
            with self._lock:
                self._active_investigations += 1
            try:
                success = investigator.investigate(ticket)
                duration = time.monotonic() - start
                self._metrics.record_investigation(duration, success)

                # Per-ticket persistence callback
                if self._on_ticket_complete:
                    try:
                        self._on_ticket_complete(ticket, "investigation", success)
                    except Exception:
                        logger.exception(
                            "on_ticket_complete callback failed for %s",
                            getattr(ticket, "ticket_id", "unknown"),
                        )

                return TaskResult(
                    ticket_id=getattr(ticket, "ticket_id", "unknown"),
                    task_type="investigation",
                    success=success,
                    duration_s=round(duration, 2),
                    ticket=ticket,
                )
            except RateLimitCooldown:
                raise  # Must propagate to daemon loop for global cooldown
            except Exception as exc:
                duration = time.monotonic() - start
                self._metrics.record_investigation(duration, False)
                error_msg = str(exc) if str(exc) else f"{type(exc).__name__} (no message)"
                logger.exception(
                    "Parallel investigation failed for %s: %s",
                    getattr(ticket, "ticket_id", "unknown"),
                    error_msg,
                )
                if self._on_ticket_complete:
                    try:
                        self._on_ticket_complete(ticket, "investigation", False)
                    except Exception:
                        pass
                return TaskResult(
                    ticket_id=getattr(ticket, "ticket_id", "unknown"),
                    task_type="investigation",
                    success=False,
                    duration_s=round(duration, 2),
                    error=error_msg,
                    ticket=ticket,
                )
            finally:
                with self._lock:
                    self._active_investigations -= 1

        assert self._investigation_pool is not None
        return self._investigation_pool.submit(_run)

    def submit_development(
        self,
        ticket: Any,
        developer: Any,
        *,
        worktree_path: Optional[str] = None,
    ) -> Future:
        """Submit a ticket for parallel development.

        Parameters
        ----------
        ticket:
            SWETicket to develop a fix for.
        developer:
            DeveloperAgent instance (must have .attempt_fix(ticket) method).
        worktree_path:
            Optional git worktree path for this worker.

        Returns
        -------
        Future that resolves to a TaskResult.
        """
        if self._shutdown:
            raise RuntimeError("Executor has been shut down")

        def _run() -> TaskResult:
            start = time.monotonic()
            with self._lock:
                self._active_developments += 1
            try:
                success = developer.attempt_fix(ticket)
                duration = time.monotonic() - start
                self._metrics.record_development(duration, success)

                if self._on_ticket_complete:
                    try:
                        self._on_ticket_complete(ticket, "development", success)
                    except Exception:
                        logger.exception(
                            "on_ticket_complete callback failed for %s",
                            getattr(ticket, "ticket_id", "unknown"),
                        )

                return TaskResult(
                    ticket_id=getattr(ticket, "ticket_id", "unknown"),
                    task_type="development",
                    success=success,
                    duration_s=round(duration, 2),
                    ticket=ticket,
                )
            except RateLimitCooldown:
                raise  # Must propagate to daemon loop for global cooldown
            except Exception as exc:
                duration = time.monotonic() - start
                self._metrics.record_development(duration, False)
                error_msg = str(exc) if str(exc) else f"{type(exc).__name__} (no message)"
                logger.exception(
                    "Parallel development failed for %s: %s",
                    getattr(ticket, "ticket_id", "unknown"),
                    error_msg,
                )
                if self._on_ticket_complete:
                    try:
                        self._on_ticket_complete(ticket, "development", False)
                    except Exception:
                        pass
                return TaskResult(
                    ticket_id=getattr(ticket, "ticket_id", "unknown"),
                    task_type="development",
                    success=False,
                    duration_s=round(duration, 2),
                    error=error_msg,
                    ticket=ticket,
                )
            finally:
                with self._lock:
                    self._active_developments -= 1

        assert self._development_pool is not None
        return self._development_pool.submit(_run)

    def collect_results(
        self,
        futures: List[Future],
        timeout: Optional[float] = None,
    ) -> List[TaskResult]:
        """Collect results from submitted futures.

        Parameters
        ----------
        futures:
            List of Future objects from submit_investigation/submit_development.
        timeout:
            Max seconds to wait for all futures. None = wait indefinitely.

        Returns
        -------
        List of TaskResult objects (one per future, in completion order).
        """
        results: List[TaskResult] = []
        try:
            for future in as_completed(futures, timeout=timeout):
                try:
                    result = future.result()
                    results.append(result)
                except RateLimitCooldown:
                    raise  # Must propagate to daemon loop for global cooldown
                except Exception as exc:
                    error_msg = str(exc) if str(exc) else f"{type(exc).__name__} (no message)"
                    logger.exception("Future raised unexpected exception: %s", error_msg)
                    results.append(TaskResult(
                        ticket_id="unknown",
                        task_type="unknown",
                        success=False,
                        duration_s=0.0,
                        error=error_msg,
                    ))
        except TimeoutError:
            # Collect partial results — don't discard completed work
            finished = sum(1 for f in futures if f.done())
            timed_out = len(futures) - finished
            logger.warning(
                "Parallel executor: %d/%d futures timed out after %ss "
                "(collected %d results before timeout)",
                timed_out, len(futures), timeout, len(results),
            )
            # Cancel remaining futures gracefully
            for f in futures:
                if not f.done():
                    f.cancel()
        return results

    def active_count(self) -> int:
        """Return the number of currently active workers."""
        with self._lock:
            return self._active_investigations + self._active_developments

    def active_investigation_count(self) -> int:
        with self._lock:
            return self._active_investigations

    def active_development_count(self) -> int:
        with self._lock:
            return self._active_developments

    def status(self) -> Dict[str, Any]:
        """Return current executor status for the API."""
        profile = self.active_profile
        return {
            "mode": self._config.mode,
            "active_profile": self._active_profile_name,
            "active_investigations": self._active_investigations,
            "active_developments": self._active_developments,
            "max_concurrent_investigations": profile.max_concurrent_investigations,
            "max_concurrent_developments": profile.max_concurrent_developments,
            "cycle_interval_seconds": profile.cycle_interval_seconds,
            "utilization": round(
                self.active_count()
                / max(1, profile.max_concurrent_investigations + profile.max_concurrent_developments),
                3,
            ),
            "metrics": self._metrics.snapshot(),
        }

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the executor pools."""
        self._shutdown = True
        if self._investigation_pool:
            self._investigation_pool.shutdown(wait=wait)
        if self._development_pool:
            self._development_pool.shutdown(wait=wait)
        logger.info("Parallel executor shut down (wait=%s)", wait)
