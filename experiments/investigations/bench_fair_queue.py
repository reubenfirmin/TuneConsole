"""Print-only fair-queue scaling study; compact drain regressions stay in tests.

./experiments/run.py investigate bench_fair_queue.py -s
"""
from statistics import median
from time import perf_counter

from tests.rec.test_weighted_fair_queue import _lane, _row
from yt_playlist.rec.surfaces import _weighted_fair_queue


def test_full_library_drain_scaling():
    timings = {}
    for size in (8000, 32000):
        elapsed = []
        for _ in range(3):
            queues = [_lane([_row(str(i)) for i in range(size)], 'large', 3),
                      _lane([_row(str(i)) for i in range(size // 2)] + [_row('small-only')], 'small')]
            hard = {str(i) for i in range(0, size, 2)}
            start = perf_counter()
            out = _weighted_fair_queue(queues, size * 2, hard, set())
            elapsed.append(perf_counter() - start)
            assert {item.key for item in out} == {str(i) for i in range(1, size, 2)} | {'small-only'}
            assert not any(q[0] for q in queues)
        timings[size] = median(elapsed)
    print(f'Fair queue full drains (median seconds): {timings}; 4x input ratio: '
          f'{timings[32000] / timings[8000]:.2f}')
