"""Low-overhead CPU spike snapshots and named background-work phases."""
from contextlib import contextmanager
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


@contextmanager
def work_span(name, *, announce=False):
    """Identify running work without sampling models or retaining frame locals."""
    token = object()
    started, cpu = time.monotonic(), time.thread_time()
    tid = threading.get_native_id()
    with _lock:
        _spans[token] = (tid, name, started)
    if announce:
        logger.info("work start: pid=%d tid=%d phase=%s", os.getpid(), tid, name)
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
        if announce or elapsed >= 2:
            logger.info("work %s: pid=%d tid=%d phase=%s elapsed=%.2fs thread_cpu=%.2fs",
                        outcome, os.getpid(), tid, name, elapsed, time.thread_time() - cpu)


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
