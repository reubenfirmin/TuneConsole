Parent: #573. Priority: **P2**. Ready.

Broader consumer checks during #623 Release integration found two unresolved
assertion failures in `tests/rec/test_shared_character_consumers.py`. These are
controlled consumer fixtures, not observations from the live library. The
fixture disables the external model, so these failures do not exercise the new
Jamendo predictor. Their earlier origin has not been independently bisected.

The fixture first failed before assertions because it wrote `mode_bundles`
through retired `rec_proposals` storage. The working tree now uses the existing
`publish_bundles` helper, retains its controlled payload for later publication,
and uses `unlocked_home_controls` for these post-unlock contracts. Actual startup
and progression are intentionally outside this fixture; #573/#613 retain that
review. No failing consumer assertion was weakened.

1. `test_prepared_profile_stamp_survives_a_new_profile_until_menu_publication`
   changes a track's explicit intensity to 0.99 and prepares updated profiles.
   Expected: the previously published menu remains identical until a new menu
   publication, including the profile stamp that explains its ordering.
   Actual: `vibe_pools.pick` returns a changed list; its first track changes to
   `library-41`, so `assert after == before` fails. Determine whether this is a
   publication/provenance regression or an obsolete contract, using #403/#573
   requirements; do not simply replace the assertion with the new behavior.
2. `test_live_and_reused_offers_preserve_current_explicit_exclusions` dislikes
   the first candidate, changes the second's duration to 100,000 seconds, and
   reclassifies the third from its prepared genre to Classical. Expected by the
   retained test: all three disappear from live and reused offers. Actual:
   `library-42`, the reclassified third candidate, remains. Reconcile the precise
   current eligibility/reclassification contract with #137/#246/#420 and verify
   live and reused paths. This does not authorize a genre-calibration redesign.

Commands and retained evidence:

```text
uv run pytest -x tests/rec/test_shared_character_consumers.py --junitxml=experiments/audio_lab/local/release-production-20261030/consumer-tests-3.xml
```

**1 passed, 1 failed in 19.84 s**; first failure above. Then:

```text
uv run pytest tests/rec/test_shared_character_consumers.py -k 'not fit_precedes_mode_heads and not prepared_profile_stamp' --junitxml=experiments/audio_lab/local/release-production-20261030/consumer-remaining.xml
```

**12 passed, 1 failed, 2 deselected in 42.63 s**; second failure above. Together
these cover the 15 cases: 13 pass, two fail. Earlier setup-failure attempts are
retained under `consumer-tests.xml` and `consumer-tests-2.xml`.

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0; current
working tree on `2a2af74d1`, no commit/push. The global suite was not run and its
lock is irrelevant to these focused checks.

Acceptance: establish the agreed publication and eligibility expectations,
repair the relevant implementation or genuinely stale fixture through ordinary
owned paths, and pass this file without weakening provenance/current-exclusion
guarantees. Record primary JUnit evidence and any affected rendered checks.
