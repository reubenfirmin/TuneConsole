"""Low-overhead CPU spike snapshots and named background-work phases."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter
import logging
import os
from pathlib import Path
import sys
import threading
import time
import traceback

logger = logging.getLogger("yt_playlist.work")
_lock = threading.Lock()
_spans = {}
_phase = ContextVar("tuneconsole_work_phase", default="unscoped")
_maintenance_phase = ContextVar("tuneconsole_maintenance_phase", default=None)


def current_phase():
    return _phase.get()


@contextmanager
def maintenance_job(phase):
    """Attribute publications to their job; a publication is progress, not completion.

    The job owner reports completed only after all requested work succeeds.
    Timing spans keep their existing names and scheduling behavior.
    """
    token = _maintenance_phase.set(phase)
    try:
        yield
    finally:
        _maintenance_phase.reset(token)


@contextmanager
def work_span(name, *, announce=False, level=logging.INFO):
    """Identify running work; callers can lower routine announcements, keeping slow work at INFO."""
    token = object()
    phase_token = _phase.set(name)
    started, cpu = time.monotonic(), time.thread_time()
    tid = threading.get_native_id()
    with _lock:
        _spans[token] = (tid, name, started)
    if announce:
        logger.log(level, "work start: pid=%d tid=%d phase=%s", os.getpid(), tid, name)
    outcome = "done"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        elapsed = time.monotonic() - started
        with _lock:
            _spans.pop(token, None)
        _phase.reset(phase_token)
        if announce or elapsed >= 2:
            logger.log(max(logging.INFO, level) if elapsed >= 2 else level,
                       "work %s: pid=%d tid=%d phase=%s elapsed=%.2fs thread_cpu=%.2fs",
                       outcome, os.getpid(), tid, name, elapsed, time.thread_time() - cpu)


class MaintenanceDiagnostics:
    """Per-store publication progress, with bounded summaries and full DEBUG detail."""

    def __init__(self, *, interval=60.0, clock=None):
        self.interval = interval
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.started = self.clock()
        self.phases = Counter()
        self.reasons = Counter()
        self.waiting = {}

    def record(self, outcome, *, reasons=(), phase=None):
        if phase is None:
            owner = _maintenance_phase.get()
            phase = owner or current_phase()
            if owner and outcome == 'published':
                outcome = 'progress'
        now = self.clock()
        with self.lock:
            self._record(outcome, reasons, phase, now)

    def resolve_wait(self, phase, reason):
        """Retire disabled/obsolete work once, without producing idle poll events."""
        with self.lock:
            if phase in self.waiting:
                self._record('obsolete', (reason,), phase, self.clock())

    def _record(self, outcome, reasons, phase, now):
        self.phases[phase, outcome] += 1
        self.reasons.update((phase, reason) for reason in set(reasons))
        if outcome == 'deferred':
            self.waiting.setdefault(phase, now)
        elif outcome in {'published', 'completed', 'obsolete'}:
            self.waiting.pop(phase, None)
        self._flush(now, force=False)

    def flush(self, *, force=False):
        with self.lock:
            self._flush(self.clock(), force=force)

    def _flush(self, now, *, force):
        elapsed = now - self.started
        if not self.phases or (not force and elapsed < self.interval):
            return
        totals = Counter()
        phases = {}
        for (phase, outcome), count in sorted(self.phases.items()):
            totals[outcome] += count
            phases.setdefault(phase, {})[outcome] = count
        reasons = {}
        for (phase, reason), count in sorted(self.reasons.items()):
            reasons.setdefault(phase, {})[reason] = count
        logger.info('Maintenance summary: interval=%.1fs published=%d deferred=%d failed=%d '
                    'phases=%s reasons=%s waiting_s=%s', elapsed, totals['published'] + totals['progress'],
                    totals['deferred'], totals['failed'], phases, reasons,
                    {phase: round(now - start, 1) for phase, start in sorted(self.waiting.items())})
        self.started = now
        self.phases.clear()
        self.reasons.clear()


class CpuDiagnostics:
    """Sample CPU counters; capture stacks only above 150% of one CPU core."""
    def __init__(self, work, *, interval=2.0, threshold=150.0, cooldown=15.0):
        self.work = work
        self.interval, self.threshold, self.cooldown = interval, threshold, cooldown
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="cpu-diagnostics", daemon=True)
        self._thread.start()
        logger.info("CPU diagnostics enabled: pid=%d sample=%.1fs threshold=%.0f%% cooldown=%.0fs",
                    os.getpid(), self.interval, self.threshold, self.cooldown)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    @staticmethod
    def _native_cpu():
        """Linux native counters include NumPy's worker threads with no Python frame."""
        counts = {}
        try:
            ticks = os.sysconf("SC_CLK_TCK")
            for path in Path("/proc/self/task").glob("*/stat"):
                try:
                    fields = path.read_text().rsplit(")", 1)[1].split()
                    counts[int(path.parent.name)] = (int(fields[11]) + int(fields[12])) / ticks
                except (OSError, ValueError, IndexError):
                    continue
        except (OSError, ValueError, AttributeError):
            pass
        return counts

    def _run(self):
        previous = (time.monotonic(), time.process_time(), self._native_cpu())
        last_report = float("-inf")
        while not self._stop.wait(self.interval):
            try:
                now, cpu, native = time.monotonic(), time.process_time(), self._native_cpu()
                elapsed = now - previous[0]
                percent = 100 * (cpu - previous[1]) / elapsed
                if percent >= self.threshold and now - last_report >= self.cooldown:
                    last_report = now
                    self._report(now, percent, elapsed, native, previous[2])
                previous = (now, cpu, native)
            except Exception:
                logger.warning("CPU diagnostics snapshot failed", exc_info=True)

    def _report(self, now, percent, elapsed, native, previous_native):
        with _lock:
            spans = list(_spans.values())
        logger.warning("CPU spike: pid=%d cpu=%.0f%% interval=%.2fs native_threads=%d active_jobs=%s phases=%s",
                       os.getpid(), percent, elapsed, len(native), self.work.active,
                       [{"tid": tid, "phase": name, "elapsed_s": round(now - start, 1)}
                        for tid, name, start in spans])
        threads = {t.ident: t for t in threading.enumerate()}
        frames = sys._current_frames()
        hot = sorted(((100 * max(0, value - previous_native.get(tid, value)) / elapsed, tid)
                      for tid, value in native.items()), reverse=True)
        logger.warning("CPU native threads: %s", [{"tid": tid, "cpu_percent": round(cpu, 1)}
                                                 for cpu, tid in hot[:16]])
        # Include non-Python numeric workers in the counters above. Their calling
        # Python thread remains here, usually inside a NumPy/SVD/PPR operation.
        active_tids = {tid for tid, _, _ in spans}
        heat = {tid: cpu for cpu, tid in hot}
        def priority(item):
            tid = getattr(threads.get(item[0]), "native_id", None)
            return tid not in active_tids, -heat.get(tid, 0)
        ordered = sorted(frames.items(), key=priority)
        for ident, frame in ordered[:20]:
            thread = threads.get(ident)
            if thread is self._thread:
                continue
            stack = " | ".join(f"{entry.filename}:{entry.lineno} in {entry.name}"
                               for entry in traceback.extract_stack(frame, limit=10))
            logger.warning("CPU Python stack: tid=%s thread=%s stack=%s",
                           getattr(thread, "native_id", ident), getattr(thread, "name", "unknown"), stack)
