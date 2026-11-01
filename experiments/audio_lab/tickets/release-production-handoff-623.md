Implemented for independent review. The user's chosen #577 aggression model is
bundled and active by default for Release suitability; no refit or #614 alternative
was substituted. Changes are uncommitted and the running application was not
restarted or deployed.

The 20,516-byte artifact has SHA256
`3155a3a5dd44776f950688f643eff4fae9ded38029dea9af7c34eead634cc0ac`.
The exact 187-song predictor reproduces NDCG **0.7111130117** on 470 Soundtracks
excerpts and **0.7112196672** on their browser-prepared inputs, with maximum
prediction error **5.55e-17** versus frozen #577 outputs. This replays inspected
anger-transfer evidence; it does not remove the original uncertainty or prove
direct Release suitability.

Release and Cruise compose independently. Shared classification/thresholds and
the other suitability scores remain unchanged. An internal shared-Release
measurement preserves radio intensity and its original measurement lineage;
the public profile still has six vibes. Artifact/mode changes use the existing
bounded rescore queue, with one original-recording vote, optional-model fallback,
and explicit rollback. Inference reads no annotations or experiment files.

Validation:

- **104 passed** for Release/external/model/embedding tests; **19 passed** for
  Cruise, plus **50 passed** preparation/intensity checks before the broader
  consumer fixture stopped that selection. Exact commands are retained in
  `experiments/audio_lab/RELEASE_PRODUCTIONIZATION.md`.
- All **84 actual frozen recordings**, including 11 incomplete captures, replayed
  through first/available excerpts: 159 cases, maximum error 1.11e-16.
- A new isolated database exercised metadata-only startup, partial captures,
  complete captures, activation and rollback under normal budgets. All 73
  complete recordings received updated Release scores. Other vibes, Cruise and
  intensity were unchanged; rollback was exact. Rescoring took 2.55 seconds of
  work, maximum slice 0.51 seconds. No live database access or migration.
- An offline-built wheel contains the exact artifacts, notice and code. A fresh
  isolated process used a real capture with both individual models, no research
  imports and network calls rejected.
- Diff whitespace check passed; 145 research Python files parsed. No global
  suite or test-lock acquisition. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy
  1.18.1, threadpoolctl 3.7.0; working tree, not CI or a release.

Retained reports are under
`experiments/audio_lab/local/release-production-20261030/`:
`package-2`, `replay-1`, `installed-1`, and test XML files. The linked report
records hashes, the exact commands, calibration limits and the scope of each
check. `package-1` preserves the original installation/export attempt.

Remaining review:

- #624 records two broader shared-consumer assertion failures uncovered after
  repairing obsolete fixture publication/readiness setup. The other 13 cases
  pass. Those tests disable external models; their origin has not been bisected.
  Full consumer acceptance is not claimed. Reuse #573's full-account/rendered
  review and #424 capture acceptance; #405/#587 continue to own output evaluation.
- The exact source artifact still records research-only terms with no inherited
  product acceptance. The pinned publisher README restricts dataset use and
  requires authorization for other uses. Its terms and attribution are retained
  in the artifact and bundled notice; distribution clearance remains unresolved.
- Independent acceptance, release/CI evidence and any eventual deployment remain
  outstanding. No new listening, shadow phase or genre-calibration work is added.
