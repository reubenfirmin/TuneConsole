# Focused test ownership

Keep checks serial and use the shared pytest lock described in the repository's
`AGENTS.md`. A busy lock belongs to the reported process; use a bounded wait or
continue editing. Do not launch browser and unit checks concurrently.

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

For example, validate Radio write settlement with
`uv run pytest tests/test_radio_writes.py tests/test_radio_recovery.py`.
Validate Taste control interactions separately with
`uv run pytest -m browser tests/browser/test_taste_controls_browser.py`.
Use `--test-lock-wait=60` if waiting for the current owner is useful.

Shared setup is explicit:

- `radio_session_support.py` owns the function-scoped `poc` fixture and playback
  helper. Controller, route and browser consumers import them directly.
- `browser/radio_ui_support.py` owns the function-scoped strict fake Radio API.
- `road_trip_support.py` owns candidate builders and `no_network`. Every split
  Road Trip module imports that autouse fixture, preserving its original scope
  without applying it to unrelated tests.
- `web_support.py`, `setup_support.py` and `taste_support.py` contain only their
  small client/runtime collaborators. They introduce no autouse fixtures.

Root and browser `conftest.py` fixture visibility remains unchanged. CI discovers
test files automatically; its deterministic file sharding includes these owners
without a hard-coded module list. The six-module split preserved 287 test
functions at the Python AST level and all 420 collected cases, including their
parametrization. A subsequent onboarding integration adjustment explicitly marks
setup complete for the empty-library settings-density fixture; its viewport and
eight-visible-controls assertions remain intact.
