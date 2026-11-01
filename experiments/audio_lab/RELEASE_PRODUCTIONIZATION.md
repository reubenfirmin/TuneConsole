# Release productionization — #623

The user selected the exact #577 Jamendo aggression predictor after reviewing
#614. It is now bundled and active by default for Release suitability. This is
an implementation handoff, not independent acceptance or a deployment.

The selected model is a 200-input ridge regression, alpha 1000, trained on 187
human-labelled Jamendo songs. Each excerpt is predicted and clipped separately,
then the available predictions are averaged. No refitting, new encoder, new
audio extraction, source reweighting or #614 alternative is included.

## Artifact and replay

`src/yt_playlist/rec/data/release-vibes.json` is **20,516 bytes**. SHA256:
`3155a3a5dd44776f950688f643eff4fae9ded38029dea9af7c34eead634cc0ac`.
Its original multi-attribute source has SHA256
`4591b47b47775fe095a5260feda4e9819abd61b9b663834595fa6c9bffebe415`;
only its `aggressive` predictor was exported. Source plan SHA256:
`c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`.

```text
./experiments/run.py release-package --directory experiments/audio_lab/local/release-production-20261030/package-2
```

The final export retains the exact product artifact and archived inference
source. Its receipt is
`7410f8f2faa9ac15e626244613492b1629c7879eaea4d2a171c74f34d0ec6903`.
`package-1` remains as the first export; its `--install` invocation copied the
same replay-verified artifact into the package.

| Fixed Soundtracks reference | NDCG | Maximum prediction replay error |
| --- | ---: | ---: |
| Historical preparation | 0.7111130117 | 5.55e-17 |
| Browser preparation | 0.7112196672 | 5.55e-17 |

Both use all 470 excerpts, grouped into 295 related identities. The target is
human anger, normalized from the original rating scale. It is not direct Release
activity suitability. The original shared comparator was 0.6216724360. The
original paired NDCG gain interval, [-0.0082, +0.1911], includes zero. The user
chose adoption with that evidence; historical retain recommendations and the
uncertainty in #577/#614 remain recorded.

All 84 frozen local recordings were also replayed: 159 first/available-excerpt
cases, including 11 incomplete captures, maximum error 1.11e-16. Local labels
were not scored. Cached-feature inference in this process took a median
0.077 ms and maximum 0.223 ms; first artifact load took 0.55 ms. The whole export
process peaked at 653 MB RSS, including its research dependencies and receipt
verification. These measurements exclude encoder, audio extraction, fresh app
startup and scheduling idle time.

## Consumer and preparation behavior

- Release and Cruise compose without replacing one another's score or provenance.
  The other four suitability scores, shared classification, margin and threshold
  remain unchanged. The aggression score is independent and uncalibrated; it
  does not enter the six-way argmax or inherit the old 0.65 threshold.
- Radio intensity retains the old shared Release score alongside Energize. A
  separate internal `shared_release` measurement uses the existing relational
  history, prediction lineage and one-vote-per-recording propagation. The public
  profile still has six vibe fits; the internal score is exposed only under
  `intensity_fits`. Explicit intensity corrections remain authoritative.
- Release artifact identity and mode participate in the existing bounded
  background queue. Replacement coefficients and continuing complete captures
  update the original recording's contribution. No annotation reads, migrations
  or request-time fitting are added.
- Missing, malformed, oversized or incompatible optional models retain the shared
  Release prediction and existing Cruise result. Incomplete captures retain the
  existing recommendation fallback until a complete capture is available; live
  previews can use available excerpts. Missing artist/genre metadata does not
  block inference.
- `YT_PLAYLIST_AUDIO_VIBE_RELEASE_MODE=off`, or an explicitly empty
  `YT_PLAYLIST_AUDIO_VIBE_RELEASE_MODEL`, restores shared Release through the same
  bounded queue. An explicit model path selects replacement coefficients.

The existing heuristic/provider/propagation fallbacks are retained. Their scales
are not newly calibrated against the aggression predictor. The external ranking
result alone does not establish mixed-coverage recommendation quality; #405/#587
retain ownership of product-output evaluation.

## Actual-capture and installed-package checks

```text
./experiments/run.py release-production --directory experiments/audio_lab/local/release-production-20261030/replay-1
uv build --offline --wheel --out-dir experiments/audio_lab/local/release-production-20261030/wheel-1
./experiments/run.py release-wheel --directory experiments/audio_lab/local/release-production-20261030/installed-1 --wheel experiments/audio_lab/local/release-production-20261030/wheel-1/yt_playlist-0.1.17.dev29+g2a2af74d1.d20261030-py3-none-any.whl
```

The production replay used a new isolated database reconstructed from the 84
frozen recordings and original capture envelopes. It never opened the live
database. Metadata-only startup, first excerpts, complete captures, activation
and rollback all converged under normal production budgets. All 73 complete
recordings received changed Release measurements. Cruise, other vibes and
intensity stayed unchanged; rollback was exact and immutable history survived.
The active rescore took 2.55 seconds of work, with a maximum slice of 0.51 seconds.
The product source hash set was unchanged during execution. Report SHA256:
`926afae2df6da5818bcfca141825cf6ed71e3ad50493031539c53d7044b31c54`.

The extracted wheel contains the exact Release/Cruise/shared artifacts, notice
and integration code. A fresh isolated Python process ran the actual preview
with a frozen capture, no research imports and network access rejected. Both
individual models applied. Installed-check report SHA256:
`f6fe45dcba02edf98352d617a7031a0e981a45057b334550e9269edf2ace233f`.

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.
These checks are author evidence on the working tree. Full-account/rendered
acceptance remains with #573 and output evaluation with #405/#587; #424 capture
acceptance is reused. No full suite, commit, push, live database migration or
running-app reload was performed.

## Focused tests and remaining consumer review

```text
uv run pytest -x -o faulthandler_timeout=40 tests/test_audio_vibe_release.py tests/test_audio_vibe_external.py tests/test_audio_vibe_model.py tests/test_audio_vibe_embeddings.py
```

**104 passed in 57.02 seconds**, including 21 Release checks. Coverage includes
the pinned artifact, clipping before aggregation, independent Cruise composition,
invalid model/input fallback, active defaults, artifact replacement, batch=1
rescoring/propagation, one measurement vote, explicit intensity corrections,
unchanged classification, absent metadata and exact rollback.

```text
uv run pytest tests/test_audio_vibe_cruise.py --junitxml=experiments/audio_lab/local/release-production-20261030/cruise-tests.xml
```

**19 passed in 5.48 seconds.** Another 50 preparation/intensity checks passed in
`tests/rec/test_vibe_prediction.py` and `tests/rec/test_musical_character.py`
before the broader selection stopped at an obsolete consumer fixture.

That fixture was converted to the existing owned publication helper and the
existing post-unlock fixture. All 15 shared-consumer cases then reached their
assertions: 13 passed and two remain unresolved, tracked in **#624** with exact
commands and JUnit files. They concern preserving a published profile snapshot
and handling a candidate's later genre reclassification. Those controlled tests
disable external models; they are not a positive or negative evaluation of the
new aggression predictor. Their origin has not been bisected. Consumer-wide
acceptance is therefore **not** claimed here.

`git diff --check` passed and `./experiments/run.py check --syntax-only` parsed
145 research Python files. All experiments and focused tests ran independently
of the global-suite lock. The first sandbox-specific pytest invocation stalled
inside an existing async TestClient; the normal approved `uv run pytest` form
completed that API coverage.

## Consistency follow-up

Consistency follow-up after the initial handoff: Cruise and Release use the same
production composition, active defaults, bounded rescore queue, original-evidence
propagation and explicit-off rollback. Release additionally retains shared Release
as an internal intensity input because radio combines the shared Energize and
Release scales. The new aggression model is not yet an intensity replacement.
The Release loader now also matches Cruise's fallback for excessively nested
artifact JSON. `uv run pytest tests/test_audio_vibe_release.py -k optional_failure`
passed **12 checks in 1.05 seconds**; `git diff --check` passed. Coefficients and
valid-input scoring are unchanged. Earlier wheel evidence predates this loader
error-handling change.

## Source terms

The bundled artifact and `release-vibes.NOTICE.md` retain the original research
terms record. The pinned MTG-Jamendo publisher README describes CC BY-NC-SA 4.0
metadata, individually licensed audio, and a research/academic restriction with
authorization required for other uses. Product/distribution clearance for this
particular derived artifact remains unresolved. Neither this implementation nor
the notice claims that clearance or extends the earlier artifact's acceptance.
