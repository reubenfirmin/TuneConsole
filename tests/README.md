# Focused test ownership

The [#534 runtime disposition ledger](runtime-review.md) records
consolidated coverage, retained integrations, benchmark ownership and targeted
verification gaps.

Test fixtures are disposable. `tmp_path` and `tmp_path_factory` data is removed
after passing and failing runs; an explicit `--basetemp` is also removed after
the session. Put reports that must survive in a separate output directory, not
inside the temporary tree. Collection-only runs leave an unused `--basetemp`
untouched.

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
