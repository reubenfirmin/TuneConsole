# Focused test ownership

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

Use the shared pytest lock described in the repository's `AGENTS.md`. A busy lock
belongs to the reported session; use a bounded wait or continue editing. Workers
join their coordinator's ownership, and keep a lifetime lock until they finish,
including after coordinator failure. Do not launch separate browser and unit
sessions concurrently.

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
Use `--test-lock-wait=60` if waiting for the current owner is useful.

Storage repair acceptance uses temporary file databases; the saved multi-gigabyte
recovery rehearsal remains opt-in. Run the server and browser checks together:

```sh
uv run pytest --test-lock-wait=60 -m '' \
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
uv run pytest --test-lock-wait=60 -m '' \
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
