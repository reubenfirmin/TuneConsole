Author campaign complete; ready for independent review. Report:
`experiments/audio_lab/OUTPUT_EVALUATION_RESULTS.md`. Exact source/runtime/model
identities, commands, ordered outputs, paired reference diagnostics, operational
limits and reused #424/#573/#641 evidence are recorded there. No new model was
trained, selected or adopted.

The same isolated account snapshot supplies all three declared evidence arms:
14,332 recordings, 2,607 current original provider measurements, 124 complete
captures and 46 missing/partial captures. Direct replay establishes **83 complete
recordings using all six pinned specialists and 41 legacy captures using the
audio heuristic because all three excerpts lack embeddings**. Every served
direct suitability/intensity score replays exactly; compatible inputs cannot
silently fall back. The initial audit's mistaken assumption that every complete
capture was embedding-compatible was corrected in the reporter, without
rerunning or changing arm outputs.

- Each arm fills all six standard Mix Tape outputs (four 12-song cards per vibe)
  and all three standard 24-song Radio episodes. All 81 explicit restriction
  cases preserve their controls, with no violations; restricted Mix sets are
  31–48 songs and Radio episodes are 16/11/16 songs, so those shorter outputs are
  reported rather than called full.
- The first-incomplete-capture combined condition reproduces metadata-only's
  exact ordered outputs and profiles. Signal-only keeps zero-support fits and
  unknown intensity. Collaborative vectors remain unchanged; consumer annotation
  read guards stay at zero. All served profiles match their declared arms.
- Fresh labels: 85 exact recording joins, 73 matching inference-window joins,
  84 previously inspected. Unselected vibes are unknown, with no invented
  negatives. The eleven external cohorts (5,725 rows) have no verified local
  recording/version/window crosswalk. Ranking, false-positive rates, unsuitable
  inclusions and whole-list benefit remain unidentified. Positive-only error and
  paired group intervals are clearly limited score-scale diagnostics.
- Combined Cruise has 25 directly specialist-scored output slots; Energize and
  Uplift each have one; Relax, Focus and Release have none. All 25 direct-audio
  Release slots use legacy heuristic captures. This routing/scale finding is now
  explicitly tracked under #642; it does not prove that those songs are wrong or
  reopen an accepted model by itself.
- Publication takes 265–276 seconds. Warm candidate/direction retunes take
  48–57 seconds, mostly CPU. Original 180-second publication and 30-second Radio
  limits failed and remain preserved alongside separately declared continuations.
  These measurements exclude HTTP, persistence and remote writes. Related
  evidence is linked to #633 without claiming a root cause of its live failure.

Validation: `./experiments/run.py check --test test_output_evaluation.py`:
**10 passed in 0.89 seconds**. The complete three-arm comparison command in the
report also passes its actual-data integrity, routing, fallback, pairing and
served-profile checks. Python 3.14.8; runtime manifest SHA256
`160c16c69627533785c94624a6efcbcc5acbdd4b6ece84a3d8af2979014ad7e2`.
Comparison SHA256:
`2c4e2c94d7869d14b387c47860a830ef5f46e0b27bad0c274b81027b3b68d52c`.
The receipt at
`experiments/audio_lab/local/output-evaluation-587-20261030/round-4/comparison-receipt.json`
pins the script, labels and three complete arm receipts.

This is prepared-account research evidence, not independent acceptance,
deployment, rendered UI, fresh-account startup or extension playback certification.
The native account was read-only; all execution and artifacts are isolated under
experiments. No global suite, commit or push. #573/#641 retain their own review;
after this report is accepted, #405 can proceed to parent closeout review without
a second experiment or manual-listening campaign.
