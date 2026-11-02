# Jamendo artist extension result, #709

**Retain; acquisition-limited and inconclusive.** No new recordings were
acquired. This run provides no evidence about additional-artist aggression
generalization and warrants no production subtask. The bounded failure is
preserved without substituting artists, expanding shards or using Soundtracks
to choose a different model.

## Frozen cohort and acquisition

The [protocol](RELEASE_EXTENSION_PLAN.md) selects **41 potential tracks / 34
additional artist IDs** from shards 70 and 63 by complete native annotation
schema, published audio checksum and identity exclusions. The complete
sixteen-source-version/local component join (original thirteen plus three
pilots) finds no known-component exclusions.
Target-independent connected-artist hashing assigns **21 tracks to extension
training and 20 to sequestered evaluation**. These are potential inventory
counts; actual verified usable support is **zero tracks / zero artists** in
both partitions. All 41 selected IDs remain listed as missing.

Both CDN requests returned **HTTP503** with zero audio bytes. The upstream
[official download script](https://github.com/MTG/mtg-jamendo-dataset/blob/master/scripts/download/download.py)
also documents an MTG origin. One bounded attempt per identical archive there
returned HTTP503 for both shards too. No tar/track checksum verification was
possible. Four requests across both endpoints received no audio; no automatic
retry/backfill followed. These failures do not establish a dataset defect or
permanent endpoint unavailability. Final-origin acquisition elapsed **3.67 s**;
the earlier failed CDN attempt is preserved separately in round 1.

No held-out aggression value was opened. The original annotation table was
available and parsed in previous audits, so even successful acquisition would
have been a new-audio/model holdout rather than a fresh annotation population.
Source whole-song votes would be approximated by one center ten-second excerpt;
that coverage limitation is frozen without claiming excerpt-specific judgments.

## Completed control diagnostic

The empty-cohort path still seals the exact adopted, original-pool refit and
extended-refit predictions. With zero extension rows the last two are identical,
as required. The refit uses all **216 original native Jamendo rows** after
held-out/local/destination exclusions; the exact adopted artifact was trained on
the earlier **187-row transfer-protected pool**. Their different predictions
reflect this disclosed original-pool difference and establish no extension gain.
Both use fixed alpha1000 aggression heads; there is no power mix or search.

Previously inspected Soundtracks anger transfer, 470 rows / 295 components:

| Preparation | Model | NDCG | Top-list lift | MSE |
|---|---|---:|---:|---:|
| Historical | Exact adopted | .76812 | .38678 | .04118 |
| Historical | Original = empty extension | .78479 | .40793 | .03979 |
| Browser | Exact adopted | .71601 | .35141 | .04410 |
| Browser | Original = empty extension | .74796 | .38578 | .04291 |

These diagnostics use the same fixed reference, grouped folds and 1,000 paired
component draws. The extension-minus-original differences and intervals are
exactly zero. All MAE, calibration and paired adopted comparisons remain in the
report. They cannot replace the missing reserved-artist evaluation. There is no
native holdout error/ranking estimate or cathartic-benefit claim.

## Evidence and verification

Final directory `local/release-extension-709-20261101/round-3/`, plan SHA256
`560aaa6e4a5da96d09af632aa7a8342ccf67a1e595fb69eb7df28e6cde700b76`.
Four receipts verify acquisition failures, empty features, sealed models and
predictions, and final report. Four focused checks and four actual-data checks
pass: target-independent artist/component partition, selective target opening,
empty-cohort behavior, exact native alpha/replay, complete exclusions, missing
record accounting, outcome order, scratch cleanup and hashes. Acquisition/code
corrections before fitting are preserved in rounds 1/2; final rules and cohort
remain fixed. The sequestered identities remain available for a future explicitly
bounded retry when downloads work.

Final extraction initialization **.94 s**, fitting **.10 s**, evaluation
**5.46 s**; no new encoder chunks. Recorded Python peak RSS **560,680,960 bytes**,
largest child **230,035,456 bytes**, largest fitted artifact **16,455 bytes**.
Temporary archive/PCM directories are absent. Python 3.14.8 / NumPy 2.4.6 /
native Essentia 2.1-beta6-dev, full runtime pinned.

```sh
./experiments/run.py release-extension freeze
./experiments/run.py release-extension run
./experiments/run.py release-extension verify
./experiments/run.py check --test test_release_extension.py
./experiments/run.py investigate release_extension_709.py
```

Use a new `--directory` for a later run, preserving this evidence. No production
edits, global-suite lock, live-database access or new listening judgments.

Source-provenance correction found during #708: the original freezer captured
imported helpers but omitted this CLI's `__main__` entrypoint. The current
`release_extension.py` is separately preserved under `postrun-source/`, with
its hash, capture time and original plan hash in `postrun-source.json`.
This is post-run reproduction code, not a prospective entrypoint hash. Original
plans, archived helpers, inputs and measured outputs remain unchanged. The
freezer is fixed for future rounds. Verification now covers five receipts,
including the additional post-run source record; the source-freeze gap remains
an explicit limitation for independent review.
