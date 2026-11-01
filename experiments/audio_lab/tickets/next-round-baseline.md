Parent: #530. Priority: **P2**.

The configured production artifact was calibrated on 55 local songs. The latest
read-only inventory has 84 usable labeled recordings, including eight recording
IDs absent from the prior 76-ID protected snapshot. Before promoting externally
trained Cruise, compare it with both the frozen product and a fairly refreshed
version of the current architecture.

Keep this research implementation and all snapshots under `experiments/`, using
`./experiments/run.py`. External human annotations remain the primary quality
reference. Local annotations are supplementary; stored predictions are not gold.

- Freeze current examples, identity groups, embedding signatures and input/code
  hashes. Separate the eight additions before fitting, close artist/recording
  groups, and audit original gold/silver and known weak-training exposure. Do
  not call an exposed or artist-overlapping addition an independent holdout.
- Refresh the existing Emotify native-head plus six-vibe calibration architecture
  using only eligible development groups. Keep the shipped artifact unchanged.
  Freeze regularization selection and source/target identity exclusions before
  fits. Preserve the frozen artifact as a second comparator.
- Compare on the same external audio/reference rows with the fixed candidate
  Cruise ensemble. Report ranking, absolute-score error, coverage, every
  declared control and conditional uncertainty. Score any reserved local groups
  once only after candidate and baseline artifacts are fixed.
- Report that this is reused-corpus confirmation and a small developer-label
  check, not a new untouched population benchmark. Preserve original evidence.

Acceptance: immutable plan/input/model/result manifests; reproducible commands;
independently checked baseline replay; explicit eligible/excluded counts and
per-vibe results; supplementary local results separated from external evidence.
No production model replacement or live-database writes.
