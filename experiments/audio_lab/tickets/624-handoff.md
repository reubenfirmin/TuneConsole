Reconciled the two failures against #403/#420 and the later #486/#487 ownership contracts. This is a fixture correction; no production scoring, publication, eligibility or schema code was changed.

The earlier first assertion called `vibe_pools.pick` twice. With #486's typed pools, that assembles a new offer using current canonical catalog/profile owners; it is not a read of the previously published Home menu. #487 owns the immutable menu publication. The revised test prepares and reads a real published Home menu through `home_prepared.prepare`, `peek` and `cards`, changes/prepares the underlying profile, and retains the exact `after == before` assertion over the served tracks. It also verifies the held profile's journey intensity, that newly assembled candidates report the current profile digest, and that a subsequent real publication changes the menu and supplies current profile stamps. This preserves the promised user-visible publication/provenance boundary without reintroducing copied profile history into candidate storage.

The second assertion assumed any new genre makes a fresh cross-genre vibe offer ineligible. #420 requires explicit restrictions and current exclusions; #486 hydrates current catalog metadata. The test now explicitly avoids Classical, then proves a song reclassified to Classical is removed alongside the disliked and overlong songs, and that the zero-weight Metal exclusion applies to both fresh assembly and reused offers. The same restriction is recorded on the reused offer's recipe. No genre calibration or exclusion policy was redesigned.

Commands and results (Python 3.14.8, current working tree):

```
uv run pytest tests/rec/test_shared_character_consumers.py --tb=short --junitxml=/tmp/tuneconsole-624-before.xml
```

Reproduced exactly the two reported failures: 13 passed, 2 failed, 69.57 seconds.

```
uv run pytest tests/rec/test_shared_character_consumers.py --tb=short --junitxml=/tmp/tuneconsole-624-after.xml
```

15 passed, 77.44 seconds. These are controlled current-schema consumer fixtures, not actual-library or rendered UI acceptance. The changed file is `tests/rec/test_shared_character_consumers.py`; original failure evidence remains intact. No global suite, native database mutation, deployment, commit or push. Ready for independent review of the contract reconciliation; #573 retains account/rendered-consumer acceptance.

Integration follow-up: the shared fixture now uses the current `publish_bundles`
helper and unlocked Home controls. Rechecked the entire file without changing
those concurrent edits:

```
uv run pytest tests/rec/test_shared_character_consumers.py --tb=short --junitxml=/tmp/tuneconsole-624-integrated.xml
```

15 passed in 71.61 seconds. The published-menu immutability, fresh-candidate
profile adoption and explicit genre restriction assertions remain in place.
