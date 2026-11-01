Author research handoff for #580, now In review at the user's request.

The original comparison is complete in
`experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md`. Frozen run:
`experiments/audio_lab/local/individual-20261030/remaining-1/`; plan SHA256
`c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`,
evaluation receipt SHA256
`156b9689e2a757f960b129c83f69c18b1aa30db7ef16bcd6cc08fa007cebb863`.

DEAM valence improved natural ManyMusic NDCG from 0.7084 to 0.8191 (paired gain
interval [0.0316, 0.1610]) and Memo from 0.9162 to 0.9522. It declined on
generated music, and Soundtracks fell from 0.7318 to 0.5533 (gain interval
[-0.2683, -0.0361]). Equal VA averaging did not repair that domain failure.
The original recommendation remains **retain** for this bounded comparison.

The report covers grouped native selection, identity-protected transfer,
exact/refreshed/browser and named-prior controls, native error, ranking/top-list
metrics, calibration, uncertainty, supported distinction slices and temporal
diagnostics. The matched primary cohorts are 171 natural ManyMusic, 95 generated,
63 Memo and 470 Soundtracks recordings/excerpts. Only one Soundtracks excerpt
met the frozen gentle-happy thresholds; broader subjective distinctions remain
unknown. The 84-song local cohort has 23 explicit Uplift positives and supplies
supplementary diagnostics only.

Prior author evidence records eight protocol checks and three independent
arithmetic/receipt checks for the shared campaign, exact export replay and
passing resource bounds. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1,
threadpoolctl 3.7.0. `./experiments/run.py individual verify` was just checked
successfully during the #579 reconciliation against the same frozen artifacts;
that unchanged passing verification is reused here. No new tests or fitting
were needed to attach this handoff.

Independent acceptance remains outstanding. #618 now owns the user's requested
mapping follow-up, including a consensus comparison. #530 supplies its shared
protocol; #573/#587 retain production/output acceptance. No production change,
new listening gate or shadow phase is part of #580 closeout.
