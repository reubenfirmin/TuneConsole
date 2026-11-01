Parent: #530. Follows #615. Priority: **P2**. **In review**. The bounded research series is complete; independent acceptance is pending. Production adoption remains deferred.

Question: how much Focus-label disagreement is associated with different tasks or listener preferences?

Use three bounded lines of evidence. First, compare pooled, task-offset and separate Reading/Studying audio heads on the same corrected CAL500 folds; test whether task-specific slopes add more than different base rates. Second, compare original same-song responses by liking and familiarity with recording-group bootstrap and within-song permutation controls. Partial `uid` fields do not establish usable listener histories for most CAL500 responses.

Third, use the retained Moods and Activities listened-example annotations, which have repeated listener IDs. Compare track/population estimates with general activity-endorsement and intellectual-activity-specific listener adjustments. Hold out whole artists for the main test, whole listeners for the cold-listener control, and user-track pairs for a supplementary known-item test; retain repeated-pair grouping. Use fixed shrinkage, prespecified sensitivity checks, crossed listener/artist uncertainty and shuffled-listener controls. Its broader intellectual label remains a separate diagnostic, never replacement Reading/Studying truth or an audio benchmark.

Audit HelpStudy episode/task support before fitting. Only 18 episodes have completed concentration/distraction responses in the retained release; do not turn the other blanks into negatives or attribute episode outcomes to tracks. Preserve this precision limit rather than fitting an unsupported task-person interaction.

Acceptance: frozen comparisons, explicit support/missingness, independent arithmetic/leakage checks, useful conclusions distinguishing task effects, endorsement tendencies, same-response associations and stable musical preferences. No claim of causal concentration benefit or personalized musical ranking without evidence. No new data collection or production changes.

## Completed author evidence

Shared plan/report: `experiments/audio_lab/FOCUS_CONTROLS_PLAN.md` and `FOCUS_CONTROLS_RESULTS.md`. Frozen root: `experiments/audio_lab/local/focus-controls-637-638-20261030/round-1/`; plan SHA256 `4417d96df39c28577f061a463ee1d8bc4d625d7e2d596e807bce1f44e31c5c0f`.

Separate Reading/Studying audio heads do not improve over shared slopes with task offsets: browser mean two-task MSE difference +0.000185 [-0.000052, 0.000426]. All three task designs have Focus NDCG 0.498598. This bounds demonstrated benefit in this model family; it does not establish identical tasks.

Within the same song, liking is associated with +14.55 percentage points Reading suitability [10.57, 18.76] and +17.42 points Studying [13.30, 21.60], across 255 mixed-liking songs and 783 known responses. Both Holm-adjusted permutation p-values are 0.0020. Familiarity is weaker and does not pass the planned corrected permutation test. CAL500 provides 1,677 responses on 494 songs, but uid on only 42 responses across 13 IDs; unknown contexts stay unknown. These are observational associations with incomplete listener-dependence control.

Moods and Activities supplies 28,524 distinct listener/song pairs from 1,604 listeners, 5,129 tracks and 2,456 artists, after merging 26 repeated pair rows. On held-out artists, population/generic-listener/intellectual-specific MSE is 0.110456 / 0.107828 / 0.095619. Specific versus generic difference is -0.012209 [-0.020118, -0.006739] under crossed listener/artist bootstrap; shuffled-listener p=0.005 (199 refits), with the same direction at all four prespecified smoothing settings. Whole-listener holdout falls back exactly to baseline. This supports activity-specific endorsement tendencies across artists, not personal musical ranking or causal concentration benefit. The broader intellectual construct remains separate from Focus.

HelpStudy has 18 complete outcomes from seven participants; three have no stated task. It remains a support audit, with no fitted interaction or episode-to-song relabeling. The missingness and task counts are retained in the report.

Eight focused checks pass; the separate arithmetic audit reproduces deduplication, all three split schemes, training-only listener probabilities (maximum error 2.23e-16), cold-listener fallback and 180 audio metrics. `./experiments/run.py focus-controls verify` passes. Listener report SHA256 `38091617be8f297587b74b5c00067b68444ba58e22c0b01308572fb3076154e3`. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0; one numerical thread, no global suite/lock. These are author workspace artifacts awaiting independent acceptance, with no commit/push or production changes.
