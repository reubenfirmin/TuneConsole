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
tests. Use `tests/browser -m browser` for the browser suite to avoid collecting
unrelated unit tests, `-n 0` for serial execution or
`-n N` for an explicit worker count. Coverage runs need explicit `-n 4` to use
workers. Coordinate full-suite ownership under [AGENTS.md](../AGENTS.md).
CI tests run only on pull requests; releases build and publish without tests.
Browser coverage has a 20-minute total pytest budget, including collection,
fixtures and teardown. CI verifies the sum of all browser shards' elapsed
times; adding runners does not increase the allowance. Select distinct useful
desktop/TV behavior by measured cost, with no fixed test-count target.
Shard timing includes final journal synchronization, report writing and pytest
cleanup. A missing final timing receipt cannot establish passing coverage.

Unit shards calibrate historical file costs, then weight ownership using a short
SQLite/Python probe on each current runner. [ci_runner_capacity.py](../ci_runner_capacity.py)
exchanges immutable manifests from the same run, attempt, revision and runtime
before pytest starts; missing manifests fail after a bounded 180-second wait.
All eight shards use the same measurements and retain them in their receipts.
Whole files still execute once in serial shards. Predicted completion times are
planning evidence; actual charged wall times establish measured balance.

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
  `./scripts/test-artifacts-cleanup.py`, then repeat with `--delete`.
  Discovery covers known temporary and fixture locations; optional `PATH...`
  arguments restrict it to named workspaces, and `--verbose` lists each path.
  Run from the host PID namespace. The command refuses live owners, unknown
  unreadable processes and active legacy test jobs, and preserves shared roots,
  reports, library baselines and lock files.
- `live_server.LiveServer` drains admitted requests and background work before
  its Store and temporary directory close. Release deliberately held work
  before stopping it; a timed thread join is not a substitute for shutdown.
- Browser profiles use a short `/tmp/tuneconsole-browser-workspace-*` alias to
  the fixture volume. Teardown removes the alias. The cleanup command also
  handles abandoned aliases after their processes stop.

### Keeping checks focused

- Avoid repeated schema construction, unrelated large fixtures, repeated model
  fits and duplicate browser journeys. Each consumer still owns a private
  database and file namespace.
- Bootstrap, migration, admission and corruption tests exercise their real
  subject operations; historical-format tests use genuine installed versions.
- Share setup for cases using the same prepared input while preserving distinct
  failure, recovery, concurrency, ownership and negative cases.
- Keep production-scale checks in explicit benchmarks. Smaller ordinary
  fixtures must retain the boundary that caused the bug.
- Covered-line overlap identifies review candidates; it does not establish
  equivalent assertions or workflows.

Historical runtime evidence: `build/test-reports/534-full-analysis/`.

## Test owners

Paths below are relative to `tests/`.

| Area | Modules |
| --- | --- |
| Radio | `test_radio_session.py`, `test_radio_writes.py`, `test_radio_recovery.py`, `test_radio_direction.py`, `test_radio_preparation.py`, `test_radio_routes.py` |
| Radio browser | `browser/test_radio_browser.py`, `browser/test_radio_tuning_browser.py`, `browser/test_radio_motion_browser.py` |
| Road Trip | `test_road_trip.py`, `test_road_trip_acquisition.py`, `test_road_trip_mixing.py`, `test_road_trip_balance.py`, `test_road_trip_ordering.py` |
| Taste | `test_taste_page.py`, `test_taste_controls_web.py`, `test_taste_evaluation_web.py`, `test_taste_feedback_web.py`; `browser/test_taste*_browser.py` |
| Web routes | `test_web.py`, `test_setup_routes.py`, `test_network_routes.py`, `test_sync_routes.py`, `test_cleanup_routes.py`, `test_merge_routes.py`, `test_enrichment_routes.py`, `test_move_routes.py` |
| Storage repair | `test_storage_maintenance.py`, `browser/test_storage_notice_browser.py` |

Shared helpers live in `radio_session_support.py`, `browser/radio_ui_support.py`,
`road_trip_support.py`, `web_support.py`, `setup_support.py` and
`taste_support.py`. Import the required fixtures explicitly.

`conftest.py` owns Playwright for browser and packaged checks. Browser-only runs
reuse the session driver; mixed runs close it after each module so subsequent
unit tests can use `asyncio.run()`.

The shared FakeClient `live_app` consumes prepared state with automatic workers
disabled and omits metadata tag-matcher configuration. Provider and startup
configuration tests construct their own app; the browser genre editor retains
its real configuration. Shared browser apps reuse source-checked Jinja bytecode
while keeping each app's environment, globals and rendered responses private.
Prepared Home and mocked Radio consumers use `home_pages_only`; dialog layout
consumers construct their real page/action routers. Cross-page integration
journeys retain the full application.

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

Use the shared local library manager for actual-library evidence.

### Local actual-library workspaces

[`scripts/test-library.py`](../scripts/test-library.py) captures SQLite and its
owned file domains once, verifies and freezes the baseline, then creates private
Btrfs snapshots for local checks. Every container sees its selected library at
`/library/state.db`; file coordinates are relocated only during preparation.
Normal runs have no donor mount. Ordinary pytest and GitHub CI do not invoke
this backend or require its tools, image or mount.

The host needs Btrfs tools, rootless Podman and the cached Playwright Python base
image named in `scripts/library-tests.Containerfile`. Prepare and mount once:

```sh
python3 scripts/test-library-image-poc.py --prepare-image
sudo bash scripts/test-library-image-poc-mount.sh
```

The mount helper is for the user to inspect and run under the privileged-command
policy in AGENTS.md. It mounts a new sparse image file inside `build/`; it does
not reformat the host filesystem. Do not repeat setup on an already mounted
volume. Build the local dependency image from `uv.lock`, then capture once:

```sh
./scripts/test-library.py build-runtime
./scripts/test-library.py prepare
./scripts/test-library.py list
```

Preparation reads the source through a read-only connection and file-lifetime
fences. A second container restores and checks every SQL file coordinate without
access to the donor. Concurrent preparation requests share a source lock.
`prepare` reuses the current generation; only `prepare --refresh` captures newer
data. `--source PATH` selects another source. A dependency-lock change requires
an explicit `build-runtime` before further runs.

Run focused pytest files or node ids through the manager. Collection shares the
frozen baseline read-only; each selected test gets its own writable snapshot and
container. The explicit plugin supplies a `managed_library` fixture. Existing
actual-library helpers and benchmark adapters reuse that path in managed runs:

```sh
./scripts/test-library.py run -- pytest tests/schema/check_radio_launch_actual.py --schema-source-db=/library/state.db -s
./scripts/test-library.py run --readonly -- -c 'import sqlite3; print(sqlite3.connect("file:/library/state.db?mode=ro", uri=True).execute("select count(*) from recordings").fetchone())'
```

Python checks can import `tests.library_image.fixtures.managed_database()`.
Use `run --baseline ID` or `run --schema VERSION` to select a retained generation;
the selection stays pinned across all selected tests, even during refresh.
Scripts must write retained artifacts to `/reports`; the host destination is
chosen with `run --report PATH` under `build/test-reports` or `/tmp`, or defaults
to a private directory under `build/test-reports/library-images`.
Console output, per-run receipts and pytest JUnit reports are retained there.
Disposable workspaces and process-local temporary files are removed after their
containers stop, on both passing and failing runs.

The manager keeps up to two recent generations, plus current generations for
other sources and any active pins. Before capturing another generation it prunes
older unused captures to leave room for the candidate; the current generation
stays available even if preparation fails. Preview and remove abandoned runs or
older unpinned generations with:

```sh
./scripts/test-library.py gc
./scripts/test-library.py gc --delete
```

`./scripts/test-library.py verify` explicitly checks concurrent actual-library
writes, extent sharing and interrupted-owner recovery. Add `--refresh` to check
new-generation publication while old workspaces remain active. This is a focused
local backend check, separate from ordinary pytest and CI.

For an individual abandoned `build/library-image-poc/volume/workspaces/ID`, use
`./scripts/test-artifacts-cleanup.py PATH` and then `--delete PATH`. Cleanup checks
the manager receipt and lifetime lease; it stops only that orphan's own container
before deleting its snapshot. Active owners and pinned generations are protected.
Baselines are retained inputs, outside `.pytest_cache`, and must be removed only
through the manager. Reports are outside the volume.

Ordinary synthetic fixtures and CI retain their existing copy behavior. Tests of
export/recovery and explicit paired-copy experiments retain those operations
because the copy or restore is part of what they measure. Do not replace their
subjects with an already restored library.

The separate `scripts/test-library-image-poc.py` measures first-write extent
sharing with representative sidecars. Its disposable `proof-*` directories use
the same stable artifact cleanup command. When finished with the local volume,
the user can unmount it with `sudo umount -- build/library-image-poc/volume`.
