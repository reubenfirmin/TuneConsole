Parent: #530. Priority: **P2**.

Cruise improves external ranking, but the current product normalizes six vibe
scores to sum to one and uses best-vibe decisions and a 0.65 threshold. The new
models produce independent suitability scores. Ranking gains alone do not make
those scales or thresholds interchangeable.

Use `experiments/` and the approved entrypoint to define and evaluate the score
contract before recommendation-policy changes.

- Inventory all consumers of normalized scores, thresholds, margins, top-vibe
  choices and shared intensity. Preserve the six product definitions and radio
  controls. Activation and aggression remain distinct measurements.
- Define independent per-vibe scores, evidence coverage and abstention. Changing
  Cruise must not renormalize the other five scores. Distinguish a best available
  song from a sufficiently strong match; allow multiple suitable vibes.
- Predeclare calibration methods and acceptance criteria. Fit calibration only
  inside external training folds with grouped nested evaluation; evaluate transfer
  without fitting on target labels. Local reserved labels remain supplementary.
- Compare calibration, ranking, unsuitable intrusions and partial-capture behavior
  against frozen and refreshed baselines. Report unsupported vibes and domains.

Acceptance: documented consumer migration contract and frozen calibration
evidence sufficient to decide Cruise promotion. Retain current Energize, Relax
and Uplift; keep Focus and Release in shadow until separately supported. Do not
reuse the old 0.65 threshold without evidence.
