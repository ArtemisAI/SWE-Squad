"""Comprehensive unit tests for src.swe_team.scheduler."""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.swe_team.scheduler import (
    JobPriority,
    JobScheduler,
    JobStatus,
    JobStore,
    ScheduleType,
    ScheduledJob,
    TimeWindow,
    cron_matches,
    next_cron_match,
    parse_cron_field,
)


# ---------------------------------------------------------------------------
# 1. parse_cron_field
# ---------------------------------------------------------------------------
class TestParseCronField(unittest.TestCase):
    def test_wildcard(self):
        self.assertEqual(parse_cron_field("*", 0, 5), [0, 1, 2, 3, 4, 5])

    def test_step(self):
        self.assertEqual(parse_cron_field("*/15", 0, 59), [0, 15, 30, 45])

    def test_step_large(self):
        self.assertEqual(parse_cron_field("*/30", 0, 59), [0, 30])

    def test_range(self):
        self.assertEqual(parse_cron_field("1-5", 0, 10), [1, 2, 3, 4, 5])

    def test_list(self):
        self.assertEqual(parse_cron_field("1,3,5", 0, 10), [1, 3, 5])

    def test_list_with_spaces(self):
        self.assertEqual(parse_cron_field("2, 4, 6", 0, 10), [2, 4, 6])

    def test_single_value(self):
        self.assertEqual(parse_cron_field("7", 0, 23), [7])

    def test_out_of_range_filtered(self):
        # Values outside [min, max] are excluded
        self.assertEqual(parse_cron_field("30", 0, 23), [])

    def test_range_and_step_combined(self):
        # A complex list: "0,30" is valid
        self.assertEqual(parse_cron_field("0,30", 0, 59), [0, 30])

    def test_wildcard_hours(self):
        result = parse_cron_field("*", 0, 23)
        self.assertEqual(len(result), 24)
        self.assertEqual(result[0], 0)
        self.assertEqual(result[-1], 23)

    def test_step_from_1(self):
        # */2 on months (1-12)
        self.assertEqual(parse_cron_field("*/2", 1, 12), [1, 3, 5, 7, 9, 11])


# ---------------------------------------------------------------------------
# 2. cron_matches
# ---------------------------------------------------------------------------
class TestCronMatches(unittest.TestCase):
    def _dt(self, minute=0, hour=0, day=1, month=1, year=2026):
        # weekday: Jan 1, 2026 = Thursday (3)
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)

    def test_every_minute(self):
        self.assertTrue(cron_matches("* * * * *", self._dt(minute=42)))

    def test_specific_minute(self):
        self.assertTrue(cron_matches("30 * * * *", self._dt(minute=30)))
        self.assertFalse(cron_matches("30 * * * *", self._dt(minute=0)))

    def test_specific_hour_minute(self):
        self.assertTrue(cron_matches("0 12 * * *", self._dt(minute=0, hour=12)))
        self.assertFalse(cron_matches("0 12 * * *", self._dt(minute=0, hour=11)))

    def test_day_of_week(self):
        # Jan 1, 2026 = Thursday = weekday 3
        self.assertTrue(cron_matches("0 0 * * 3", self._dt()))
        self.assertFalse(cron_matches("0 0 * * 1", self._dt()))

    def test_day_of_month(self):
        self.assertTrue(cron_matches("0 0 1 * *", self._dt(day=1)))
        self.assertFalse(cron_matches("0 0 15 * *", self._dt(day=1)))

    def test_specific_month(self):
        self.assertTrue(cron_matches("0 0 * 1 *", self._dt(month=1)))
        self.assertFalse(cron_matches("0 0 * 6 *", self._dt(month=1)))

    def test_invalid_field_count(self):
        self.assertFalse(cron_matches("* * *", self._dt()))
        self.assertFalse(cron_matches("* * * * * *", self._dt()))

    def test_every_30_min(self):
        self.assertTrue(cron_matches("*/30 * * * *", self._dt(minute=0)))
        self.assertTrue(cron_matches("*/30 * * * *", self._dt(minute=30)))
        self.assertFalse(cron_matches("*/30 * * * *", self._dt(minute=15)))

    def test_range_in_hour(self):
        self.assertTrue(cron_matches("0 9-17 * * *", self._dt(hour=12)))
        self.assertFalse(cron_matches("0 9-17 * * *", self._dt(hour=8)))


# ---------------------------------------------------------------------------
# 3. next_cron_match
# ---------------------------------------------------------------------------
class TestNextCronMatch(unittest.TestCase):
    def test_every_minute(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        result = next_cron_match("* * * * *", after=base)
        self.assertEqual(result.minute, 1)

    def test_every_hour(self):
        base = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        result = next_cron_match("0 * * * *", after=base)
        self.assertEqual(result.hour, 1)
        self.assertEqual(result.minute, 0)

    def test_specific_time(self):
        base = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        result = next_cron_match("30 14 * * *", after=base)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)

    def test_past_time_rolls_to_next_day(self):
        base = datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)
        result = next_cron_match("0 0 * * *", after=base)
        self.assertEqual(result.day, 2)
        self.assertEqual(result.hour, 0)

    def test_returns_within_48h(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        result = next_cron_match("*/5 * * * *", after=base)
        self.assertTrue(result - base < timedelta(hours=48))


# ---------------------------------------------------------------------------
# 4. TimeWindow
# ---------------------------------------------------------------------------
class TestTimeWindow(unittest.TestCase):
    def setUp(self):
        self.tw = TimeWindow()  # peak 13-19 UTC, Mon-Fri

    def test_peak_during_peak_hours_weekday(self):
        # Wednesday 15:00 UTC
        dt = datetime(2026, 1, 7, 15, 0, tzinfo=timezone.utc)  # Wed
        self.assertTrue(self.tw.is_peak(dt))

    def test_not_peak_before_start(self):
        dt = datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc)  # Wed 10am
        self.assertFalse(self.tw.is_peak(dt))

    def test_not_peak_after_end(self):
        dt = datetime(2026, 1, 7, 19, 0, tzinfo=timezone.utc)  # Wed 7pm (boundary)
        self.assertFalse(self.tw.is_peak(dt))

    def test_not_peak_on_weekend(self):
        dt = datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc)  # Saturday
        self.assertFalse(self.tw.is_peak(dt))

    def test_peak_at_boundary_start(self):
        dt = datetime(2026, 1, 7, 13, 0, tzinfo=timezone.utc)  # Wed 1pm
        self.assertTrue(self.tw.is_peak(dt))

    def test_peak_at_boundary_end_minus_one(self):
        dt = datetime(2026, 1, 7, 18, 59, tzinfo=timezone.utc)
        self.assertTrue(self.tw.is_peak(dt))

    def test_next_off_peak_when_not_peak(self):
        dt = datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(self.tw.next_off_peak(dt), dt)

    def test_next_off_peak_during_peak(self):
        dt = datetime(2026, 1, 7, 15, 0, tzinfo=timezone.utc)  # Wed 3pm
        result = self.tw.next_off_peak(dt)
        self.assertEqual(result.hour, 19)
        self.assertEqual(result.minute, 0)

    def test_custom_window(self):
        tw = TimeWindow(peak_start_hour=9, peak_end_hour=12, peak_days=[0, 1])
        dt_peak = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # Monday
        dt_off = datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc)   # Wednesday
        self.assertTrue(tw.is_peak(dt_peak))
        self.assertFalse(tw.is_peak(dt_off))


# ---------------------------------------------------------------------------
# 5. ScheduledJob to_dict / from_dict round-trip
# ---------------------------------------------------------------------------
class TestScheduledJobSerialization(unittest.TestCase):
    def test_round_trip(self):
        job = ScheduledJob(
            job_id="test-123",
            name="test job",
            description="a test",
            schedule_type=ScheduleType.CRON,
            cron_expression="*/5 * * * *",
            priority=JobPriority.HIGH,
            instructions="do stuff",
            status=JobStatus.SCHEDULED,
            max_retries=5,
            metadata={"key": "value"},
        )
        d = job.to_dict()
        restored = ScheduledJob.from_dict(d)
        self.assertEqual(restored.job_id, "test-123")
        self.assertEqual(restored.name, "test job")
        self.assertEqual(restored.schedule_type, ScheduleType.CRON)
        self.assertEqual(restored.priority, JobPriority.HIGH)
        self.assertEqual(restored.status, JobStatus.SCHEDULED)
        self.assertEqual(restored.max_retries, 5)
        self.assertEqual(restored.metadata, {"key": "value"})

    def test_to_dict_enum_values(self):
        job = ScheduledJob(
            schedule_type=ScheduleType.INTERVAL,
            status=JobStatus.RUNNING,
            priority=JobPriority.CRITICAL,
        )
        d = job.to_dict()
        self.assertEqual(d["schedule_type"], "interval")
        self.assertEqual(d["status"], "running")
        self.assertEqual(d["priority"], "critical")

    def test_from_dict_ignores_unknown_keys(self):
        data = {"job_id": "x", "name": "y", "unknown_field": 42}
        job = ScheduledJob.from_dict(data)
        self.assertEqual(job.job_id, "x")

    def test_from_dict_defaults(self):
        job = ScheduledJob.from_dict({})
        self.assertEqual(job.priority, JobPriority.NORMAL)
        self.assertEqual(job.status, JobStatus.PENDING)


# ---------------------------------------------------------------------------
# 6. JobStore
# ---------------------------------------------------------------------------
class TestJobStore(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            jobs = [
                ScheduledJob(job_id="a", name="job A"),
                ScheduledJob(job_id="b", name="job B"),
            ]
            store.save_all(jobs)
            loaded = store.load_all()
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].job_id, "a")
            self.assertEqual(loaded[1].job_id, "b")

    def test_load_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "nonexistent.json")
            self.assertEqual(store.load_all(), [])

    def test_upsert_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            job = ScheduledJob(job_id="new", name="new job")
            store.upsert(job)
            loaded = store.load_all()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].name, "new job")

    def test_upsert_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            job = ScheduledJob(job_id="upd", name="original")
            store.upsert(job)
            job.name = "updated"
            store.upsert(job)
            loaded = store.load_all()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].name, "updated")

    def test_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "jobs.json"
            p.write_text("NOT VALID JSON{{{")
            store = JobStore(p)
            self.assertEqual(store.load_all(), [])

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "sub" / "dir" / "jobs.json")
            store.save_all([ScheduledJob(job_id="x")])
            self.assertEqual(len(store.load_all()), 1)

    def test_string_path_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(tmp + "/jobs.json")
            store.save_all([])
            self.assertEqual(store.load_all(), [])


# ---------------------------------------------------------------------------
# 7. JobScheduler.should_run
# ---------------------------------------------------------------------------
class TestShouldRun(unittest.TestCase):
    def _make_scheduler(self, time_window=None, quota_checker=None):
        with tempfile.TemporaryDirectory() as tmp:
            pass
        # We use a store but won't persist
        store = JobStore(Path(tmp) / "jobs.json")
        return JobScheduler(store=store, time_window=time_window, quota_checker=quota_checker)

    def _make_job(self, **kw):
        defaults = dict(
            job_id="j1",
            name="test",
            schedule_type=ScheduleType.CRON,
            cron_expression="* * * * *",
            status=JobStatus.SCHEDULED,
            enabled=True,
            priority=JobPriority.NORMAL,
            respect_peak_hours=True,
        )
        defaults.update(kw)
        return ScheduledJob(**defaults)

    def test_runs_when_due(self):
        sched = self._make_scheduler()
        job = self._make_job(next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        ok, reason = sched.should_run(job)
        self.assertTrue(ok)
        self.assertEqual(reason, "ready")

    def test_not_due_yet(self):
        sched = self._make_scheduler()
        job = self._make_job(next_run=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        ok, reason = sched.should_run(job)
        self.assertFalse(ok)
        self.assertIn("not due", reason)

    def test_disabled_job(self):
        sched = self._make_scheduler()
        job = self._make_job(enabled=False)
        ok, _ = sched.should_run(job)
        self.assertFalse(ok)

    def test_wrong_status(self):
        sched = self._make_scheduler()
        job = self._make_job(status=JobStatus.COMPLETED)
        ok, _ = sched.should_run(job)
        self.assertFalse(ok)

    def test_defers_normal_during_peak(self):
        # Create a time window where it's always peak (0 <= hour < 24)
        tw = TimeWindow(peak_start_hour=0, peak_end_hour=24, peak_days=list(range(7)))
        sched = self._make_scheduler(time_window=tw)
        job = self._make_job(
            priority=JobPriority.NORMAL,
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        ok, reason = sched.should_run(job)
        self.assertFalse(ok)
        self.assertIn("peak", reason)

    def test_defers_low_during_peak(self):
        tw = TimeWindow(peak_start_hour=0, peak_end_hour=24, peak_days=list(range(7)))
        sched = self._make_scheduler(time_window=tw)
        job = self._make_job(
            priority=JobPriority.LOW,
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        ok, reason = sched.should_run(job)
        self.assertFalse(ok)
        self.assertIn("peak", reason)

    def test_critical_ignores_peak(self):
        tw = TimeWindow(peak_start_hour=0, peak_end_hour=24, peak_days=list(range(7)))
        sched = self._make_scheduler(time_window=tw)
        job = self._make_job(
            priority=JobPriority.CRITICAL,
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        ok, reason = sched.should_run(job)
        self.assertTrue(ok)

    def test_high_allowed_during_peak(self):
        tw = TimeWindow(peak_start_hour=0, peak_end_hour=23, peak_days=list(range(7)))
        sched = self._make_scheduler(time_window=tw)
        job = self._make_job(
            priority=JobPriority.HIGH,
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        ok, reason = sched.should_run(job)
        self.assertTrue(ok)

    def test_quota_exhausted_blocks(self):
        sched = self._make_scheduler(quota_checker=lambda: (False, 0))
        job = self._make_job(
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            respect_peak_hours=False,
        )
        ok, reason = sched.should_run(job)
        self.assertFalse(ok)
        self.assertIn("quota exhausted", reason)

    def test_low_priority_needs_headroom(self):
        sched = self._make_scheduler(quota_checker=lambda: (True, 5))
        job = self._make_job(
            priority=JobPriority.LOW,
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            respect_peak_hours=False,
        )
        ok, reason = sched.should_run(job)
        self.assertFalse(ok)
        self.assertIn("insufficient quota headroom", reason)

    def test_low_priority_with_enough_headroom(self):
        sched = self._make_scheduler(quota_checker=lambda: (True, 20))
        job = self._make_job(
            priority=JobPriority.LOW,
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            respect_peak_hours=False,
        )
        ok, reason = sched.should_run(job)
        self.assertTrue(ok)

    def test_already_running_blocked(self):
        sched = self._make_scheduler()
        job = self._make_job(
            next_run=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        sched._running_jobs[job.job_id] = True
        ok, reason = sched.should_run(job)
        self.assertFalse(ok)
        self.assertIn("already running", reason)


# ---------------------------------------------------------------------------
# 8. JobScheduler._advance_schedule
# ---------------------------------------------------------------------------
class TestAdvanceSchedule(unittest.TestCase):
    def _make_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
        return JobScheduler(store=store)

    def test_once_completed(self):
        sched = self._make_scheduler()
        job = ScheduledJob(schedule_type=ScheduleType.ONCE, status=JobStatus.RUNNING)
        sched._advance_schedule(job)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertIsNone(job.next_run)

    def test_cron_advances_to_next(self):
        sched = self._make_scheduler()
        job = ScheduledJob(
            schedule_type=ScheduleType.CRON,
            cron_expression="0 12 * * *",
            status=JobStatus.RUNNING,
        )
        sched._advance_schedule(job)
        self.assertEqual(job.status, JobStatus.SCHEDULED)
        self.assertIsNotNone(job.next_run)
        next_dt = datetime.fromisoformat(job.next_run)
        self.assertEqual(next_dt.hour, 12)
        self.assertEqual(next_dt.minute, 0)

    def test_interval_adds_minutes(self):
        sched = self._make_scheduler()
        job = ScheduledJob(
            schedule_type=ScheduleType.INTERVAL,
            interval_minutes=15,
            status=JobStatus.RUNNING,
        )
        before = datetime.now(timezone.utc)
        sched._advance_schedule(job)
        self.assertEqual(job.status, JobStatus.SCHEDULED)
        next_dt = datetime.fromisoformat(job.next_run)
        # Should be ~15 minutes from now
        delta = next_dt - before
        self.assertGreater(delta.total_seconds(), 14 * 60)
        self.assertLess(delta.total_seconds(), 16 * 60)


# ---------------------------------------------------------------------------
# 9. JobScheduler._execute_job
# ---------------------------------------------------------------------------
class TestExecuteJob(unittest.TestCase):
    def test_success_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            executor = MagicMock()
            sched = JobScheduler(store=store, executor=executor)
            job = ScheduledJob(
                job_id="exec1",
                name="success job",
                schedule_type=ScheduleType.ONCE,
                status=JobStatus.SCHEDULED,
            )
            store.upsert(job)
            sched._execute_job(job)

            executor.assert_called_once_with(job)
            self.assertEqual(job.status, JobStatus.COMPLETED)
            self.assertEqual(job.run_count, 1)
            self.assertIsNone(job.last_error)
            self.assertNotIn(job.job_id, sched._running_jobs)

    def test_failure_with_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            executor = MagicMock(side_effect=RuntimeError("boom"))
            sched = JobScheduler(store=store, executor=executor)
            # Set stop_event so backoff waits return immediately
            sched._stop_event.set()
            job = ScheduledJob(
                job_id="fail1",
                name="fail job",
                schedule_type=ScheduleType.ONCE,
                status=JobStatus.SCHEDULED,
                max_retries=2,
                retry_backoff_seconds=0,
            )
            store.upsert(job)
            sched._execute_job(job)

            # 1 initial + 2 retries = 3 calls
            self.assertEqual(executor.call_count, 3)
            self.assertEqual(job.status, JobStatus.FAILED)
            self.assertIn("boom", job.last_error)
            self.assertEqual(job.run_count, 1)

    def test_success_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            call_count = {"n": 0}

            def flaky_executor(j):
                call_count["n"] += 1
                if call_count["n"] < 3:
                    raise RuntimeError("transient")

            sched = JobScheduler(store=store, executor=flaky_executor)
            sched._stop_event.set()  # skip backoff waits
            job = ScheduledJob(
                job_id="flaky1",
                name="flaky job",
                schedule_type=ScheduleType.ONCE,
                status=JobStatus.SCHEDULED,
                max_retries=3,
            )
            store.upsert(job)
            sched._execute_job(job)

            self.assertEqual(job.status, JobStatus.COMPLETED)
            self.assertEqual(call_count["n"], 3)


# ---------------------------------------------------------------------------
# 10. JobScheduler.add_job
# ---------------------------------------------------------------------------
class TestAddJob(unittest.TestCase):
    def test_add_cron_job_sets_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            sched = JobScheduler(store=store)
            job = ScheduledJob(
                job_id="add1",
                name="cron job",
                schedule_type=ScheduleType.CRON,
                cron_expression="0 * * * *",
            )
            result = sched.add_job(job)
            self.assertEqual(result.status, JobStatus.SCHEDULED)
            self.assertIsNotNone(result.next_run)

    def test_add_interval_job_sets_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            sched = JobScheduler(store=store)
            job = ScheduledJob(
                job_id="add2",
                name="interval job",
                schedule_type=ScheduleType.INTERVAL,
                interval_minutes=10,
            )
            before = datetime.now(timezone.utc)
            result = sched.add_job(job)
            self.assertIsNotNone(result.next_run)
            next_dt = datetime.fromisoformat(result.next_run)
            self.assertGreater(next_dt, before)

    def test_add_once_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            sched = JobScheduler(store=store)
            job = ScheduledJob(
                job_id="add3",
                name="once job",
                schedule_type=ScheduleType.ONCE,
            )
            result = sched.add_job(job)
            self.assertEqual(result.status, JobStatus.SCHEDULED)

    def test_add_persists_to_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.json")
            sched = JobScheduler(store=store)
            job = ScheduledJob(job_id="persist1", name="persisted")
            sched.add_job(job)
            loaded = store.load_all()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].job_id, "persist1")


# ---------------------------------------------------------------------------
# 11. CRUD operations
# ---------------------------------------------------------------------------
class TestCRUD(unittest.TestCase):
    def _make_scheduler_and_job(self):
        tmp = tempfile.mkdtemp()
        store = JobStore(Path(tmp) / "jobs.json")
        sched = JobScheduler(store=store)
        job = ScheduledJob(job_id="crud1", name="crud test")
        sched.add_job(job)
        return sched, job

    def test_get_job(self):
        sched, job = self._make_scheduler_and_job()
        found = sched.get_job("crud1")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "crud test")

    def test_get_job_not_found(self):
        sched, _ = self._make_scheduler_and_job()
        self.assertIsNone(sched.get_job("nonexistent"))

    def test_list_jobs_all(self):
        sched, _ = self._make_scheduler_and_job()
        jobs = sched.list_jobs()
        self.assertEqual(len(jobs), 1)

    def test_list_jobs_by_status(self):
        sched, _ = self._make_scheduler_and_job()
        jobs = sched.list_jobs(status=JobStatus.SCHEDULED)
        self.assertEqual(len(jobs), 1)
        jobs = sched.list_jobs(status=JobStatus.FAILED)
        self.assertEqual(len(jobs), 0)

    def test_pause_job(self):
        sched, job = self._make_scheduler_and_job()
        result = sched.pause_job("crud1")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, JobStatus.PAUSED)
        # Verify persisted
        loaded = sched.get_job("crud1")
        self.assertEqual(loaded.status, JobStatus.PAUSED)

    def test_pause_nonexistent(self):
        sched, _ = self._make_scheduler_and_job()
        self.assertIsNone(sched.pause_job("nope"))

    def test_resume_job(self):
        sched, job = self._make_scheduler_and_job()
        sched.pause_job("crud1")
        result = sched.resume_job("crud1")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, JobStatus.SCHEDULED)

    def test_resume_non_paused(self):
        sched, _ = self._make_scheduler_and_job()
        # Job is SCHEDULED, not PAUSED
        result = sched.resume_job("crud1")
        self.assertIsNone(result)

    def test_cancel_job(self):
        sched, _ = self._make_scheduler_and_job()
        result = sched.cancel_job("crud1")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, JobStatus.CANCELLED)

    def test_cancel_nonexistent(self):
        sched, _ = self._make_scheduler_and_job()
        self.assertIsNone(sched.cancel_job("nope"))

    def test_cancelled_job_wont_run(self):
        sched, _ = self._make_scheduler_and_job()
        sched.cancel_job("crud1")
        job = sched.get_job("crud1")
        ok, _ = sched.should_run(job)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
