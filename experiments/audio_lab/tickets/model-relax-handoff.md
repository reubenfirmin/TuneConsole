Reconciled the original #579 experiment before leaving Relax. This is an author
handoff, not independent acceptance. The board showed In progress, the body
still said Backlog, and no results comment had been attached.

The bounded comparison is complete in
`experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md` (Relax, calibration,
distinctions and artifact sections). Frozen run:
`experiments/audio_lab/local/individual-20261030/remaining-1/`.
Plan SHA256:
`c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`.
Evaluation receipt SHA256:
`156b9689e2a757f960b129c83f69c18b1aa30db7ef16bcd6cc08fa007cebb863`.

Evidence against the agreed scope:

- The frozen campaign compared grouped native and protected-transfer embedding,
  acoustic and combined heads, constants, the equal VA ensemble, exact prior,
  frozen/refreshed shared and historical/browser controls. The primary matched
  cohorts contain 171 natural ManyMusic recordings, 95 generated recordings,
  63 Memo recordings, 470 Soundtracks excerpts and 428 CAL500 activity rows.
- Emotify embedding Relax versus frozen shared NDCG is 0.7850/0.7610 natural,
  0.8722/0.8607 generated, 0.8936/0.8469 Memo and 0.7958/0.5799 Soundtracks.
  The three pilot gain intervals include zero. Soundtracks gain is +0.2159
  [0.1174, 0.3013], but the named prior combined model is higher there at 0.8170.
  Equal VA averaging fails Soundtracks and reciprocal VA Relax transfer is below
  random. The original decision therefore remains **retain for this experiment**.
- Reports retain ranking/top-list metrics, MSE, calibration bins, folds and
  paired uncertainty. Source-native scores are not calibrated probabilities.
  The prior combined model cannot be compared on pilot caches missing its
  historical acoustic features; that comparison remains explicitly unavailable.
- Existing annotated slices and Memo first/three/all-excerpt trajectories
  address supported distinctions. Quietness/distraction labels, empty
  high-anger/low-energy slices and subjective outcomes remain unknown.
  The 84-song local cohort has 15 explicit Relax positives and 11 partial
  captures; it is supplementary and previously inspected.
- The original author report records eight protocol checks and three independent
  arithmetic/receipt checks across the campaign, exact head/export replay and
  passing resource limits. These prior checks were not rerun for this ticket
  update. Runtime recorded there: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1,
  threadpoolctl 3.7.0.

Current check: `./experiments/run.py individual verify` passed against the frozen
plan, inputs, archived code, references, all eight fit receipts and evaluation
receipt. The first invocation exposed an obsolete global-lock wrapper; it
completed, then the wrapper was removed and the command verified successfully
without acquiring the global-suite lock. The experiment and its archived
source/results were not changed or refitted.

Relationship to subsequent work: #617 owns the anchored consensus follow-up and
its fixed-blend recommendation; #629 owns the user's chosen production model.
Keep those decisions separate from this original retain result. Move #579 to
In review for independent assessment of the frozen comparison; do not mark it
Done solely because the later model has been integrated. Remaining full-account
and output acceptance stay with #573/#587 as already agreed.
