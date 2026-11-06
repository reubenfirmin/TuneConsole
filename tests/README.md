# Tests

## Commands

```sh
uv run pytest tests/test_radio_writes.py tests/test_radio_recovery.py
uv run pytest -m browser tests/browser/test_taste_controls_browser.py
./experiments/run.py check
```

Focused checks stay serial and do not take the global-suite lock. Full local
runs use up to four workers, keeping files together:

```sh
uv run pytest --test-lock-wait=60
uv run pytest -m '' -n 4 --test-lock-wait=60
```

The first command selects unit tests; the second includes desktop/TV browser
tests. Use `-m browser` for the browser suite, `-n 0` for serial execution or
`-n N` for an explicit worker count. Coverage runs need explicit `-n 4` to use
workers. Coordinate full-suite ownership under [AGENTS.md](../AGENTS.md).
CI tests run only on pull requests; releases build and publish without tests.

## Fixtures

- Calculation and policy tests use detached values or repository doubles.
  SQL, restart, concurrency and publication tests use real private databases.
- Ordinary consumers copy the genuine empty current-schema template through
  `store`, `file_backed_store` or `initialize_consumer`. Installed-format tests
  use genuine versioned templates. Bootstrap and migration tests build their
  own subject state. Never substitute a template for the operation under test.
- `tmp_path` and `tmp_path_factory` own disposable fixtures. The default base is
  a unique directory under `build/pytest`; `--fixture-root=PATH` changes its
  parent and `--basetemp=PATH` selects an exact base. Use distinct bases for
  concurrent runs. Passing and failing runs clean their fixtures.
- Keep retained reports outside the fixture tree. `.pytest_cache` is only for
  pytest's cache. Multi-command experiments use `TemporaryDirectory` and clean
  up on success and failure.
- After a killed run, preview abandoned work with
  `./scripts/test-artifacts-cleanup.py PATH`, then repeat with `--delete`.
  The command refuses live owners and preserves shared lock files.
- `live_server.LiveServer` drains admitted requests and background work before
  its Store and temporary directory close. Release deliberately held work
  before stopping it; a timed thread join is not a substitute for shutdown.
- Browser profiles use a short `/tmp/tuneconsole-browser-workspace-*` alias to
  the fixture volume. Teardown removes the alias. The cleanup command also
  handles abandoned aliases after their processes stop.

## Test owners

Paths below are relative to `tests/`.

| Area | Modules |
| --- | --- |
| Radio | `test_radio_session.py`, `test_radio_writes.py`, `test_radio_recovery.py`, `test_radio_direction.py`, `test_radio_preparation.py`, `test_radio_routes.py` |
| Radio browser | `browser/test_radio_browser.py`, `browser/test_radio_tuning_browser.py`, `browser/test_radio_motion_browser.py` |
| Road Trip | `test_road_trip.py`, `test_road_trip_acquisition.py`, `test_road_trip_mixing.py`, `test_road_trip_balance.py`, `test_road_trip_ordering.py` |
| Taste | `test_taste_page.py`, `test_taste_controls_web.py`, `test_taste_evaluation_web.py`, `test_taste_feedback_web.py`; `browser/test_taste*_browser.py` |
| Web routes | `test_web.py`, `test_setup_routes.py`, `test_network_routes.py`, `test_sync_routes.py`, `test_cleanup_routes.py`, `test_merge_routes.py`, `test_enrichment_routes.py`, `test_move_routes.py` |
| Storage repair | `test_storage_maintenance.py`, `browser/test_storage_maintenance_browser.py` |

Shared helpers live in `radio_session_support.py`, `browser/radio_ui_support.py`,
`road_trip_support.py`, `web_support.py`, `setup_support.py` and
`taste_support.py`. Import the required fixtures explicitly.

`conftest.py` owns Playwright for browser and packaged checks. Browser-only runs
reuse the session driver; mixed runs close it after each module so subsequent
unit tests can use `asyncio.run()`.

## Diagnostics

```sh
uv run pytest -m '' -n 4 --test-lock-wait=60 \
  --test-runtime-target=900 \
  --test-progress=/tmp/tuneconsole-diagnostics/progress.jsonl \
  --cov --cov-context=test --cov-report= \
  --junitxml=/tmp/tuneconsole-diagnostics/junit.xml \
  --durations=30 -o faulthandler_timeout=30
```

`--test-progress` records starts and phase outcomes, including worker IDs.
`--test-runtime-target=900` stops a run when it falls behind the completion
pace by more than the default 90-second allowance; change that with
`--test-runtime-slack=SECONDS`. Focused runs can instead use
`--test-time-limit=SECONDS`. Teardown and report saving can extend past either
limit. Interrupted or unstarted cases remain unverified.

Each concurrent coverage run needs its own directory and data basename.
`coverage.sqlite3` and `coverage.sqlite3.focused` conflict because coverage
manages dotted suffixes. The early `pytest_coverage_guard` plugin rejects
conflicts; never delete its locks under `/tmp/tuneconsole-coverage-<uid>/`.
Alternate pytest configurations must load `-p pytest_coverage_guard` too.
Use `--cov-config=PATH` with a separate `[run] data_file` and `core=ctrace` for
per-test contexts. `--no-cov` needs no coverage reservation.

After the run:

```sh
.venv/bin/python -m coverage html -d /tmp/tuneconsole-diagnostics/html
.venv/bin/python scripts/test-coverage-overlap.py .coverage \
  --progress=/tmp/tuneconsole-diagnostics/progress.jsonl \
  --output=/tmp/tuneconsole-diagnostics/overlap.json
```

Overlap defaults to test bodies; `--phase=setup`, `teardown` or `all` includes
fixture work. Python line overlap does not measure JavaScript, uninstrumented
subprocesses or assertion equivalence. Coverage instrumentation changes runtime;
use complete uninstrumented hosted runs for performance acceptance.

Set `TUNECONSOLE_VISUAL_REVIEW=1` to retain optional review screenshots. Pixel
assertions and browser failure artifacts retain their normal capture behavior.
Phone viewports and mobile emulation are out of scope.

## Benchmarks and investigations

`tests/bench_*.py` are opt-in recurring acceptance checks:

| Benchmark | Contract |
| --- | --- |
| `bench_svd_background.py` | Full, partial and unchanged publication; foreground availability |
| `bench_taste_groups.py` | Selected grouping engine's production budgets and lineage; needs frozen input |
| `bench_policy_retention.py` | Production retention scale and storage bounds |
| `bench_musical_preparation.py` | 96-recording throughput and indexed queries |
| `bench_public_flatpak.py` | Published signed repository and both architectures |

Historical comparisons and saved-library investigations live in
`experiments/investigations/`. Run them explicitly:

```sh
./experiments/run.py investigate bench_taste_groups.py -s
./experiments/run.py investigate bench_fair_queue.py -s
```

Use isolated copies or read-only snapshots for actual-library evidence.
The [runtime findings](runtime-review.md) summarize the historical cost review.
