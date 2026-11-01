# Focus representation, task and listener experiments: #637 / #638

The bounded series are complete. Useful Focus signal survives outside the nine
fitted emotion directions. The incremental ranking benefit of adding it to the
emotion representation remains uncertain. Listener-specific endorsement patterns
are supported; separate Reading/Studying audio slopes do not improve this round.
These are author research results awaiting independent acceptance. Production
adoption remains deferred; #615's direct export remains the potential candidate.

## Frozen design and support

The pre-fit design is [FOCUS_CONTROLS_PLAN.md](FOCUS_CONTROLS_PLAN.md). Frozen root:
`local/focus-controls-637-638-20261030/round-1/`.

- Plan SHA256: `4417d96df39c28577f061a463ee1d8bc4d625d7e2d596e807bce1f44e31c5c0f`.
- CAL500: 494 songs, 483 conservative identity components, 1,677 responses;
  unchanged 450 common evaluation songs and corrected #615 outer folds.
- Emotify: 400 songs, all nine native emotion targets. Global exclusions leave
  386, 389, 321, 372 and 382 native training songs in the five outer fits.
- Moods and Activities: 28,550 responses collapsed into 28,524 listener/song
  pairs; 1,604 listeners, 5,129 tracks and 2,456 artist IDs. Twenty-six repeated
  pair rows are merged before splitting. One resulting intellectual label is
  fractional; squared error is against the merged response fraction.
- HelpStudy: 280 episodes from ten participants, but only 18 complete
  concentration/distraction outcomes from seven participants.

These populations have already been inspected. New folds or comparisons do not
create fresh confirmation. Whole-song CAL500 labels remain paired with one short
audio excerpt. The broader intellectual-activity label remains a separate
annotation diagnostic, with no verified audio/excerpt join and no new Focus rows.

## Representation series (#637)

All maps select mean or ridge strength inside training folds, using native MSE.
Predicted representations use nested cross-fitting and complete cross-source
component exclusions. All fits use historical audio preparation; both historical
and browser inputs are evaluated. NDCG and top-list lift are fold means on the
common 450 songs. Top lists contain ten songs per fold. Higher NDCG/lift and lower
MSE are better. Focus is the fixed equal mean of Reading and Studying.

| Representation | Historical NDCG | Browser NDCG | Browser top-list lift | Browser Focus MSE | Browser mean two-task MSE |
|---|---:|---:|---:|---:|---:|
| Nine predicted emotions | 0.423936 | 0.432874 | 0.097484 | 0.019845 | 0.030456 |
| Embedding residual | 0.501370 | 0.486086 | 0.102960 | 0.019567 | 0.029908 |
| Emotions plus residual | 0.525445 | 0.512183 | 0.122127 | 0.019200 | 0.029581 |
| Unclipped emotions | 0.422595 | 0.434409 | 0.097484 | 0.019797 | 0.030422 |
| Direct embedding (#615) | 0.497920 | 0.498598 | 0.117960 | 0.018822 | 0.029163 |
| Nine PCA coordinates | 0.523216 | 0.523477 | 0.120460 | 0.018989 | 0.029385 |
| Fixed random nine-dimensional projection | 0.454897 | 0.463951 | 0.087960 | 0.020428 | 0.030802 |
| Training mean | 0.228405 | 0.228405 | 0.000000 | 0.021727 | 0.031881 |

The nine-emotion control is a newly refitted, globally protected research
representation with a CAL500 activity map. It is **not** the shared production
Focus scorer, whose browser NDCG was 0.399852 in #615.

The primary contrast, emotions plus residual versus emotions, improves browser
NDCG by **0.079309**, conditional 95% interval **[-0.039471, 0.166253]**.
Top-list lift changes by 0.024643 [-0.026371, 0.067953]; Focus MSE changes by
-0.000645 [-0.001477, 0.000156]. Historical NDCG improves by 0.101509, but its
interval also crosses zero. The frozen positive-interval criterion is not met.

Residual alone beats the mean by **0.257681 NDCG [0.173964, 0.362488]**, with
Focus MSE improvement -0.002160 [-0.003544, -0.000962]. All five native emotion
slope matrices have rank nine; residuals have negligible linear response along
those slopes. This establishes useful predictive signal outside the fitted
linear emotion directions. Orthogonality does not establish statistical
independence from emotion, nor does it establish an incremental ranking win over
emotion predictions. Cross-fit variation and transfer also affect these maps.

Direct and PCA have lower Focus MSE than the emotion map: differences -0.001022
[-0.001844, -0.000200] and -0.000856 [-0.001636, -0.000082], respectively. Their
NDCG contrasts remain uncertain. Nine PCA coordinates doing well suggests the
choice of representation matters beyond dimensionality alone; it does not make
PCA the selected production model. Removing clipping changes NDCG only 0.001535;
0.45%–2.36% of browser native emotion outputs were clipped across folds. Clipping
does not explain the large point-estimate gap in this comparison.

The direct predictions exactly reproduce #615. Keep its existing potential
candidate; this series does not authorize replacing it with the largest observed
NDCG score. The report retains all native Reading/Studying metrics, fold results,
selections and paired comparisons in `evaluation.json`.

## Task and listener series (#638)

### Reading versus Studying audio models

| Task model | Browser Reading MSE | Browser Studying MSE | Browser Focus NDCG |
|---|---:|---:|---:|
| Shared pooled head | 0.025345 | 0.032684 | 0.498598 |
| Shared slopes, separate task offsets | 0.025194 | 0.032749 | 0.498598 |
| Separate task heads (#615 direct) | 0.025495 | 0.032831 | 0.498598 |

Separate heads versus offsets change pooled mean two-task MSE by **+0.000185**
[-0.000052, 0.000426], slightly worse at the point estimate; historical change
is +0.000190 [-0.000054, 0.000431]. The prespecified improvement criterion is not
met. Pooled and separate models select the same strong regularization; linear
averaging then largely preserves their combined ranking. This is no evidence
that the tasks are identical: 51 songs have Reading votes only, 77 Studying only,
and 86 both. It is a limit on demonstrated benefit from task-specific slopes in
these data and this model family. The MSE contrast here pools rows; the table
above uses equal fold weighting, accounting for their small numerical difference.

### Same-song liking and familiarity

Each comparison averages Yes-minus-No suitability rates within recordings that
contain both known response strata, then weights recordings equally. Unknown
answers are excluded. Intervals bootstrap recording components; permutation
tests shuffle context within recordings and adjust all four p-values by Holm.

| Context / task | Recordings / responses | Suitability difference | Conditional 95% interval | Adjusted p |
|---|---:|---:|---:|---:|
| Liking / Reading | 255 / 783 | +14.55 pp | [10.57, 18.76] pp | 0.0020 |
| Liking / Studying | 255 / 783 | +17.42 pp | [13.30, 21.60] pp | 0.0020 |
| Familiarity / Reading | 189 / 650 | +4.59 pp | [0.22, 9.51] pp | 0.1089 |
| Familiarity / Studying | 189 / 650 | +3.62 pp | [-1.33, 8.69] pp | 0.1514 |

Liking is strongly associated with suitability for the same recording.
Familiarity does not survive the planned permutation/multiplicity check.
Neither association is causal or proves actual concentration benefit. Shared
response style and reverse causality remain plausible explanations.

CAL500 has listener uid on **42 of 1,677 responses**, spanning 13 IDs; most have
only one response. The 300 No_Opinion liking answers and 34 Uncertain familiarity
answers remain unknown. The annotation suffix is not a listener ID. Cross-song
listener dependence cannot be fully accounted for with these identities.

### Repeated-listener endorsement

Fixed primary smoothing is 20 pseudo-observations. The generic adjustment uses
the listener's other eight activity endorsements; the specific adjustment uses
their intellectual-activity history. All histories use training folds only.

| Split | Population/track MSE | Generic listener MSE | Intellectual-specific listener MSE |
|---|---:|---:|---:|
| Held-out artists (primary) | 0.110456 | 0.107828 | 0.095619 |
| Held-out listeners | 0.110381 | 0.110381 | 0.110381 |
| Held-out listener/song pairs | 0.110128 | 0.107361 | 0.095295 |

The specific adjustment improves primary MSE over generic by **0.012209**;
the difference interval is **[-0.020118, -0.006739]**. Relative to the population
baseline it reduces error by about **13.4%**, difference -0.014837
[-0.024174, -0.008925]. These use crossed listener/artist bootstrap weights.
Generic versus baseline has an interval crossing zero despite its positive
point improvement: -0.002629 [-0.012706, 0.006945].

Both planned within-track shuffled-listener tests have one-sided p=0.005
(199 refits); the specific-over-generic result is also supported by the crossed
interval. Its direction holds at all prespecified smoothing values 1, 5, 20 and
100. Those sensitivity settings are reported without selecting a new winner.
For the 25,228 primary rows with at least five prior training tracks, specific
MSE is 0.092544 versus baseline 0.108820. The 236 primary rows without any prior
listener responses fall back exactly to the baseline. Holding out entire
listeners likewise gives zero personalization gain, as required.

This supports a stable, activity-specific **endorsement tendency** across artists.
The model applies one listener adjustment; it does not learn which musical
styles a person concentrates to or change their within-listener ranking. The
split is not chronological and does not demonstrate future-session benefit.
Song sampling, shared response tendencies and the broader intellectual label
remain limitations. No percentage of total Focus disagreement is causally
attributed to listeners by this analysis.

### HelpStudy support limit

There are 45 stated study tasks, but 262 of 280 episodes lack complete outcome
responses. Of the 18 complete episodes, three have no stated study task; none
of the complete task labels is Computer programming. Six report positive
concentration, one negative concentration and two distraction. These are
separate checkbox responses, not exclusive classes. No task/person interaction
was fitted, no blanks became negatives, and episode outcomes were not assigned
to tracks. Seven participants with 18 outcomes cannot establish the intended
personal task-by-music model.

## Verification and handoff

- `./experiments/run.py check --test test_focus_controls.py`: **8 passed**.
  An initial test collection attempt failed on relative imports; imports were
  corrected before freezing or fitting. No fit failed and no result was discarded.
- `./experiments/run.py focus-controls freeze`, `fit`, `listeners`, `evaluate`
  and `verify` completed. All 80 nested native-training exclusion checks pass.
  Direct-baseline and ordinary replay differences are exactly zero.
- `.venv/bin/python -B -m experiments.audio_lab_tools.focus_controls_audit`
  independently reconstructed raw linear inference, residual projection, task
  outputs, tie-aware ranking metrics, deduplicated activity responses, listener
  probabilities, artist/listener isolation and cold-listener fallback. Maximum
  model error 2.09e-16, 180 metric checks with maximum error 1.12e-16, listener
  probability error 2.23e-16. This post-fit audit source is separately hashed;
  it performs no fitting or selection.
- Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0;
  one numerical thread. No global suite or test lock was used. No production
  code, database, model, commit or push was changed by this series.
- Artifact SHA256: `evaluation.json`
  `a1472c01d38eb3f32df727c38e56e64b1b24189b8b38a556c014c39797352a55`;
  `listeners.json`
  `38091617be8f297587b74b5c00067b68444ba58e22c0b01308572fb3076154e3`;
  `independent-audit.json`
  `5d0ae8dff0d9346f466441efbf93668fa1da73d430337e078606f0b7fb6fd336`.

Receipts preserve input/source hashes, fold models, nested identities, selections,
predictions and uncertainty. These are retained workspace artifacts, not pushed
repository evidence. The independent audit above means an independent arithmetic
implementation by the author; it is not acceptance by a separate review agent.
Keep #637/#638 In review for independent acceptance of the frozen scope,
exclusions, calculations and conclusions. #615's potential direct candidate and
deferred production decision remain unchanged.
