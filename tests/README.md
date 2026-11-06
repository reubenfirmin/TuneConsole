# Focused test ownership

The [#534 runtime disposition ledger](runtime-review.md) records
consolidated coverage, retained integrations, benchmark ownership and targeted
verification gaps.

Calculation and policy checks should consume detached values or repository doubles.
Examples include `rec/test_listening_calculation.py`, `rec/test_selection_policy.py`,
`rec/test_readiness_policy.py`, `test_sync_policy.py`, and
`test_taste_modes_discover.py`; they do not create a Store or SQLite fixture.
Keep SQL behavior, installed-format compatibility, atomic publication and resource
cleanup in repository integration checks. Real workflow tests remain appropriate
for concurrency, restart and component handoff contracts; do not replace those
with mocks merely to reduce runtime.

Test fixtures are disposable. `tmp_path` and `tmp_path_factory` data is removed
after passing and failing runs; an explicit `--basetemp` is also removed after
the session. Put reports that must survive in a separate output directory, not
inside the temporary tree. Collection-only runs leave an unused `--basetemp`
untouched.

Browser launches use a short private alias to that same fixture volume for
`TMPDIR`, keeping Chromium's Unix singleton socket within its path limit. The
alias is removed at session teardown; profiles and temporary data remain owned
by pytest. After a killed run, its `/tmp/tuneconsole-browser-workspace-*` alias
directory can be inspected and removed with `scripts/test-artifacts-cleanup.py`.

Live ASGI fixtures use `tests.live_server.LiveServer` inside their Store and
temporary-directory lifetime. It closes request admission and waits for the
server's request, background-task and application shutdown before returning.
Do not replace that boundary with a timed thread join: reaching a timeout does
not make it safe to close the Store or remove native evidence. Release any
deliberately held test work before closing its server. Register the owner for
cleanup immediately when fixture setup starts more than one server.

Historical comparisons, provider pilots and manual saved-library reports live in
`experiments/investigations/`. Run one explicitly through the research runner,
which loads the shared pytest fixtures without selecting the ordinary suite:

```sh
./experiments/run.py investigate bench_taste_groups.py -s
./experiments/run.py investigate bench_fair_queue.py -s
./experiments/run.py check --syntax-only
```

Files named `tests/bench_*.py` remain opt-in recurring acceptance checks. In
particular, `bench_taste_groups.py` retains the selected engine's production
budgets and lineage (requires the documented frozen input); `bench_svd_background.py`
retains real full-generation and foreground-availability checks. Policy retention
at production defaults lives in `bench_policy_retention.py`; the 96-recording
throughput/index contract lives in `bench_musical_preparation.py`. Select those
paths explicitly with `uv run pytest`; ordinary discovery does not run them.

Diagnostic screenshots in Home controls, genre badges, Radio motion and adaptive
groups are opt-in. Set `TUNECONSOLE_VISUAL_REVIEW=1` in the calling environment
before running a focused browser check to retain those captures. Browser failure
artifacts and tests that assert image pixels keep their existing behavior.

Keep `.pytest_cache` for pytest's own cache entries. For experiments that need
database copies across several commands, wrap the whole experiment in a
`tempfile.TemporaryDirectory` context. Use a disk-backed location when needed
and clean the workspace on success and failure. If an experiment requires the
workspace under `.pytest_cache` for its isolation checks, it must still use that
context and remove the workspace on exit. Do not keep dated database or source
copies between runs. A killed process cannot run cleanup; remove its abandoned
workspace after its processes stop. Retain the small report separately.

Full local runs use up to four pytest-xdist worker processes by default, bounded
by available CPUs. `uv run pytest` runs the full unit suite; `uv run pytest -m ''`
includes browser tests, and `uv run pytest -m browser` runs the full browser suite.
Files stay together on one worker to preserve module fixtures. Workers each use
one native numerical thread to avoid multiplying BLAS/OpenMP pools.

Use `uv run pytest -n 0` for a serial full run, or `-n N` to choose the worker count.
Focused paths, `-k`/other marker selections, last-failed checks, collection,
debugging, live/package acceptance and CI shards stay serial. CI keeps its isolated
runner sharding. `pytest-xdist` starts separate worker processes; a fork per test
would add process startup cost without providing this parallel execution.

Only global-suite runs use the shared pytest lock described in `AGENTS.md`.
Focused checks use `uv run pytest <paths>` directly and stay serial; they do not
acquire or wait for that lock. Global workers join their coordinator's ownership
and keep a lifetime lock until they finish, including after coordinator failure.

For live progress and per-test Python line coverage, run:

```sh
uv run pytest -m '' -n 4 --test-lock-wait=60 \
  --test-runtime-target=900 \
  --test-progress=/tmp/tuneconsole-diagnostics/progress.jsonl \
  --cov --cov-context=test --cov-report= \
  --junitxml=/tmp/tuneconsole-diagnostics/junit.xml \
  --durations=30 -o faulthandler_timeout=30
```

This includes desktop/TV browser cases and explicitly selects four local workers.
With coverage enabled, omitting `-n` stays serial because pytest-cov selects its
controller before the repository's automatic worker selection runs.
Use explicit paths for a focused diagnostic. `--test-progress` flushes timestamped
starts and setup/call/teardown outcomes to the console and JSONL, including xdist
worker identities. Test output stays captured for failure diagnostics. Each CI
shard also retains these live receipts in its evidence artifact. For serial
debugging, `--capture=tee-sys` additionally passes Python prints through live;
xdist does not forward arbitrary worker stdout live.

Every concurrent coverage run needs a disjoint data basename. For example,
`coverage.sqlite3` conflicts with `coverage.sqlite3.focused` in the same
directory because coverage erases and merges dotted suffix files. Use separate
report directories or sibling names such as `coverage-focused.sqlite3`.
The early `pytest_coverage_guard` plugin reserves the resolved `[run] data_file` before
pytest-cov can erase it, including its parallel suffix files. A conflicting run
fails immediately with the destination and owner PID; disjoint destinations
remain independent of each other and of the global-suite lock. The owner's
local xdist workers share its reservation and retain protection if their
coordinator dies. Locks release on process exit; never delete their stable files
under `/tmp/tuneconsole-coverage-<uid>/`. `--cov-append` still requires ownership.
`--no-cov` does not reserve a destination. When replacing `addopts` or using an
alternate pytest configuration, load the early plugin with `-p pytest_coverage_guard` too.

For example, create a separate report directory and coverage configuration for a
focused run while another run is active:

```sh
mkdir -p /tmp/tuneconsole-focused-coverage
cat > /tmp/tuneconsole-focused-coverage/coverage.ini <<'EOF'
[run]
core = ctrace
relative_files = true
data_file = /tmp/tuneconsole-focused-coverage/coverage.sqlite3
EOF
uv run pytest tests/providers/test_descriptive_tags.py \
  --cov=yt_playlist --cov-context=test --cov-report= \
  --cov-config=/tmp/tuneconsole-focused-coverage/coverage.ini \
  --test-progress=/tmp/tuneconsole-focused-coverage/progress.jsonl \
  --junitxml=/tmp/tuneconsole-focused-coverage/junit.xml
```

Choose a different directory for each concurrent run. Reusing it after its owner
and workers exit starts a fresh coverage recording; add `--cov-append` only when
intentionally continuing that recording.

`--test-runtime-target=900` compares elapsed wall time with the completed fraction
of all selected tests against the 15-minute target. It allows 10% of that target
(90 seconds) of lag for startup and uneven test costs. The allowance moves forward
as tests finish; a healthy run continues beyond its initial allowance. Override
the permitted lag with `--test-runtime-slack=SECONDS`. When progress falls behind
the line by more than that allowance, the coordinator records the active test
names/phases and ten slowest observed cases in a `pace_stop` receipt and the
console. Its own workers then interrupt themselves from its private stop flag.
No terminal input is needed. Collection also consumes the startup allowance.

Focused diagnostic sessions can instead use `--test-time-limit=SECONDS` for a
fixed wall-clock bound. The two modes are mutually exclusive and disabled when
omitted. Both require POSIX interval timers and refuse an existing timer. Pytest
finishes teardown and saves reports after interruption; cleanup can extend
beyond the stopping point.
When Playwright is dispatching a synchronous call or waiting for an event, the
monitor cancels its awaitable through the browser event loop. It raises the stop
only after Playwright returns control to the synchronous caller. The stop log
retains the operation, suspended caller and running thread stacks. Driver
startup/shutdown and fixture finalization finish before a pending stop is
reported, preserving tracing, admitted requests and native publication.
The maintained real-TV interruption regression has a separate process-group
owner with a 35-second fallback and retained diagnostics; a forced termination
fails that regression. This fallback belongs to that regression's supervisor,
not to every monitored pytest invocation.
Collection receipts record the selected test and file counts once per session.
Diagnostic workers also checkpoint coverage after each completed test;
the active test can still lose unsaved coverage during shutdown. A forced kill
can lose unsaved coverage. Partial runs remain partial;
neither coverage nor progress receipts make their unstarted tail a passing run.
Diagnostic finalization also saves contexts when `--maxfail` stops at a failure.
Native SQLite callbacks can translate interruption into a database exception;
the runtime monitor retains its stop reason and reports that run as interrupted.
Coverage instrumentation adds cost, so use an uninstrumented complete hosted run
for the 15-minute CI acceptance target.

After completion or a graceful interruption, inspect the retained `.coverage`:

```sh
.venv/bin/python -m coverage html -d /tmp/tuneconsole-diagnostics/html
.venv/bin/python scripts/test-coverage-overlap.py .coverage \
  --progress=/tmp/tuneconsole-diagnostics/progress.jsonl \
  --output=/tmp/tuneconsole-diagnostics/overlap.json
```

The HTML line annotations list the exact tests and execution phases. The overlap
summary gives every observed test's phase durations, outcomes, shared/exclusive
source-line counts and groups with identical covered lines. It defaults to test
bodies (`run`); choose `--phase=setup`, `teardown` or `all` to inspect fixture work.
Rows are ordered by the selected phase's time and also show the whole test's time.
It reads the coverage database directly to avoid the large repeated context lists
in JSON exports; existing `coverage json --show-contexts` exports also work.
Counts are relative to the observed contexts, including failed or interrupted
work. Module/session fixture construction is attributed to the first test that
requests it; lines exclusive to that setup context need not be unique behavior
of that test. They measure Python lines, not assertion equivalence, JavaScript execution
or uninstrumented subprocesses. Review retained behavior before consolidating
cases with matching lines. The configured C tracer is intentional: Python 3.14's
default sysmon coverage core does not record dynamic per-test contexts.

The former large feature modules retain their main topic. Related behavior now
has explicit owners, so selecting only the old file no longer selects every
test for that feature:

| Area | Test modules |
| --- | --- |
| Radio controller | `test_radio_session.py` (launch and presence), `test_radio_writes.py`, `test_radio_recovery.py`, `test_radio_direction.py`, `test_radio_preparation.py`, `test_radio_routes.py` |
| Radio browser | `browser/test_radio_browser.py` (launch and recovery), `browser/test_radio_tuning_browser.py`, `browser/test_radio_motion_browser.py` |
| Road Trip | `test_road_trip.py` (persistence), `test_road_trip_acquisition.py`, `test_road_trip_mixing.py`, `test_road_trip_balance.py`, `test_road_trip_ordering.py` |
| Web routes | `test_web.py` (shell, security and navigation), `test_setup_routes.py`, `test_network_routes.py`, `test_sync_routes.py`, `test_cleanup_routes.py`, `test_merge_routes.py`, `test_enrichment_routes.py`, `test_move_routes.py` |
| Taste routes | `test_taste_page.py` (overview and readings), `test_taste_controls_web.py`, `test_taste_evaluation_web.py`, `test_taste_feedback_web.py` |
| Taste browser | `browser/test_taste_browser.py` (overview and feedback), `browser/test_taste_learning_browser.py`, `browser/test_taste_health_browser.py`, `browser/test_taste_controls_browser.py` |
| Storage repair | `test_storage_maintenance.py` (real WAL compaction, radio, playback and recording), `browser/test_storage_maintenance_browser.py` (radio launch and startup notice through completion or low-space deferral) |

For example, validate Radio write settlement with
`uv run pytest tests/test_radio_writes.py tests/test_radio_recovery.py`.
Validate Taste control interactions separately with
`uv run pytest -m browser tests/browser/test_taste_controls_browser.py`.
Use `--test-lock-wait=60` only for a global-suite run.

Storage repair acceptance uses temporary file databases; the saved multi-gigabyte
recovery rehearsal remains opt-in. Run the server and browser checks together:

```sh
uv run pytest -m '' \
  tests/test_storage_maintenance.py tests/browser/test_storage_maintenance_browser.py
```

The browser case covers 520- and 720-pixel window heights with successful and
deferred compaction. It holds repair active, clicks the named Start Radio button
outside the separate song-title link, checks the real start response and extension
navigation within one five-second deadline, and confirms the session appears
without recording activity. It then checks the notice clears when recording
resumes, including a reload before polling. Each case writes
`radio-launch-diagnostics.json` under its pytest temporary directory with request
and response timing, the start response, bridge frames and browser console events.
Failures add server/UI state and a screenshot, and print the diagnostic path.

Shared setup is explicit:

- `conftest.py` builds read-only, empty historical SQL templates lazily per
  version and worker for `core/test_predecessor_writes.py`'s installed-layout
  consumers. Every consumer still reopens and admits its own file database,
  keeps private file namespaces, validates its unchanged migration history and
  closes its audit reader. Templates continue genuine historical migrations
  from the closest available older layout; they reject retained file coordinates.
  Populated upgrades, bootstrap and migration fault injection use their own
  builders. `test_installed_fixture.py` checks parity and isolation against
  independent genuine replay, including a lower-version request after a higher one.
- `radio_session_support.py` owns the function-scoped `poc` fixture and playback
  helper. Controller, route and browser consumers import them directly.
- `browser/radio_ui_support.py` owns the function-scoped strict fake Radio API.
- `road_trip_support.py` owns candidate builders and `no_network`. Every split
  Road Trip module imports that autouse fixture, preserving its original scope
  without applying it to unrelated tests.
- `web_support.py`, `setup_support.py` and `taste_support.py` contain only their
  small client/runtime collaborators. They introduce no autouse fixtures.

`tests/conftest.py` owns Playwright's driver and dependent browser fixtures for
both `tests/browser/` and packaged browser cases elsewhere in `tests/`. Dedicated
`-m browser` runs reuse the session driver; mixed runs close it after each module
so later unit tests can use `asyncio.run()`. Keep this focused lifecycle regression
in this order: an opt-in Flatpak skip, a live browser, then an asyncio unit test.
It requires no Flatpak build:

```sh
uv run pytest -m '' \
  tests/test_flatpak_packaged.py::test_signed_packaged_update_restarts_same_tab_and_preserves_library \
  tests/browser/test_home_vibe_browser.py::test_open_vibe_tab_resets_when_tuning_expires \
  tests/test_job_retention.py::test_sse_reconnect_replays_only_unseen_events_and_signals_expiry
```

CI discovers test files automatically; its deterministic file sharding includes these owners
without a hard-coded module list. The six-module split preserved 287 test
functions at the Python AST level and all 420 collected cases, including their
parametrization. A subsequent onboarding integration adjustment explicitly marks
setup complete for the empty-library settings-density fixture; its viewport and
eight-visible-controls assertions remain intact.
