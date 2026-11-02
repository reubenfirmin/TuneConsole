# SiTunes feasibility results (#813)

Retain the production models. Initial human mood is predictive of recorded
post-listening mood; this study does not establish a useful audio intervention
model. Context adds no reliable gain over initial mood. Supplied music descriptors
do not reliably improve either held-out evaluation and worsen later-stage valence
prediction. No productionization child is justified.

## Cohort and provenance

Primary release: [SiTunes at the pinned revision](https://github.com/JiayuLi-997/SiTunes_dataset/tree/aea53feca9ba092406b5b95a3976dba0b44c568f).
Interpretation follows its [schema](https://github.com/JiayuLi-997/SiTunes_dataset/blob/aea53feca9ba092406b5b95a3976dba0b44c568f/SiTunes/README.md)
and [psychological-satisfaction experiment](https://github.com/JiayuLi-997/SiTunes_dataset/blob/aea53feca9ba092406b5b95a3976dba0b44c568f/Experiments/Psychological_satisfaction_prediction/README.md).
Every downloaded file matches the pinned Git blob. The two additional environment
files total 145,985 bytes; their SHA256 values are in the plan input/reference maps.

| Stage | Rows | People | Items | Missing item metadata | Other missing fields |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 600 | 30 | 25 | 63 rows | 12 durations; all initial-mood ratings unavailable |
| 2 | 897 | 30 | 105 | 90 rows | 133 durations; 77 recommendation-policy labels |
| 3 | 509 | 10 | 217 | 0 | None in the interaction CSV |

The paired cohort is **1,406 rows, 30 people, 305 items/track groups**, with 1,403
distinct participant/item pairs. Each person contributes 11–128 observations.
Stage 1 remains in the audit but never enters a paired fit. All paired human VA
values are finite and within native [-1,1]. All 1,406 environment joins are exact;
stage/interaction IDs are unique. The metadata has 936 unique item IDs and no
duplicate literal artist/title pairs. Ninety paired observations lack all 20
numeric music descriptors; training-only median imputation retains these rows.
There is no claim of artist independence or verified recording identity.

UTC interaction ranges: Stage 1 June 30, 2021–February 26, 2022; Stage 2 July 1,
2021–March 3, 2022; Stage 3 April 6–25, 2022. All ten Stage 3 participants occur
in Stage 2; 17 Stage 3 tracks occur there, covering 47/509 later observations.
The later-stage comparison tests a selected returning cohort with mostly new
items, not new participants or a randomized intervention.

## Frozen comparisons

[Protocol](SITUNES_PLAN.md). Five ridge/constant families compare initial VA,
context, their combination, participant identity and supplied music descriptors.
Context includes stage/policy, time and weather. Ratings, preferences, realized
listening duration, post-mood, IDs of tracks/interactions and timestamps are not
predictors. GPS and wrist signals are excluded. Metadata item duration is allowed;
Spotify valence is an input descriptor, never the human outcome. Environment
collection timing is not independently verified, so context fits remain conditional
observational diagnostics.

There are 30 participant outer partitions, five track outer partitions and one
Stage-2/Stage-3 partition. Every outer training set has three grouped inner folds.
Category vocabularies, imputation, scaling and ridge selection use only the
appropriate training partition. All exported heads and assignments are retained.
No final deployable head is fitted. Participant and track tests estimate different
generalization settings; neither excludes both simultaneously.

Native [-1,1] post-mood MSE, with valence / arousal in each cell (lower is better):

| Predictor | New participant | New track | Stage 2 to Stage 3 |
| --- | --- | --- | --- |
| Training mean | .108373 / .138391 | .106870 / .135792 | .102238 / .150338 |
| Unchanged initial mood | .076296 / .112596 | .076296 / .112596 | .064870 / .107270 |
| Participant mean | .108373 / .138391 | .090771 / .113683 | .086904 / .126969 |
| Initial VA regression | **.056185 / .080817** | .056083 / .079616 | .047967 / .082694 |
| Context only | .109426 / .141031 | .105019 / .132213 | .101890 / .150338 |
| Initial VA plus context | .056462 / .081121 | .056266 / .078757 | .048938 / .089796 |
| Plus participant identity | .056778 / .083353 | .054675 / .072455 | .048165 / .081278 |
| Plus music descriptors | .056281 / .081067 | .054014 / .072768 | .058370 / .077469 |

The report also retains MAE, participant-balanced MSE and every participant's loss.
For new participants the participant-mean baseline correctly falls back to the
training global mean. Participant identity can still affect ridge regularization
on training rows; it supplies no known held-out participant coefficient.

Paired MSE differences below use 1,000 two-way participant/track pigeonhole draws.
Separate participant- and track-cluster intervals are also retained. These are
conditional intervals for fitted predictions, omitting training uncertainty and
any multiplicity adjustment:

- Initial-VA regression versus unchanged mood, new participants: valence
  **-.020111 [-.032181, -.010149]**, arousal **-.031779 [-.046349, -.016159]**.
  New tracks give similar differences: -.020214 / -.032980.
- Context added to initial VA, new participants: +.000278 / +.000304; both
  intervals cross zero. New-track differences +.000184 / -.000858 also cross zero.
- Participant identity added to initial/context, new tracks: valence -.001592
  [-.005516, .001875], arousal -.006302 [-.011215, -.001363]. This is a limited
  personalization signal for known people, not evidence of a better music effect.
- Music descriptors added to that model: new-participant differences -.000497 /
  -.002286 and new-track differences -.000661 / +.000313, all intervals crossing
  zero. In the later stage, valence worsens **+.010205 [.001641, .019657]**;
  arousal -.003809 [-.016595, .006750] remains uncertain.

Observed average changes are +.032917 valence and +.024590 arousal. Pre/post
correlations are .692534/.646505; pre/change correlations are -.513596/-.543440.
Change subtracts pre-mood by definition; the latter correlation and average changes
do not identify a causal recommendation effect or regression-to-the-mean mechanism.
There is no untreated comparison here. VA change supplies neither anger-relief
truth nor evidence of improved cognitive performance.

## Audio feasibility

The twelve artist/title candidates across all stages cover only **39 paired rows
from 18 people on 11 tracks**. No recording or listened interval is verified.
`audio-audit.json` retains source IDs, available audio hashes, durations and sampled
intervals without using their emotion labels for this study.

Every candidate has a duration discrepancy or is a short preview: for example,
Bon Jovi's publisher item is 249.293 seconds, while available source files range
from 15.073 to 189.492 seconds; the Bryan Adams item is 211.826 seconds versus
251.481 seconds in the existing recording. All retained feature windows are ten
seconds, but SiTunes provides no original audio, edition identifiers or listening
window offsets with which to align them. Similar titles or lengths would not
establish alignment either. The pinned repository tree has no audio assets or
explicit license file. Do not infer redistribution rights from public availability.

Broad audio recovery is not worthwhile for this tiny unverified intersection.
Use SiTunes to test initial-state/context assumptions; any larger audio project
first needs author-provided recording identity, interval alignment and clear terms.
No general audio model was trained here.

## Reproduction and checks

Final artifacts: `local/situnes-813-20261101/round-2/`. Plan SHA256:
`4b211e0743a44a001e6a8e3144952dde5b5d9eeb12fd480e542467c518b45ee4`.
Round 1 stopped before any fit because missing metadata used non-JSON NaN values.
Round 2 serialized missing values as null and froze the same statistical protocol.

Fitting: **7.07 seconds**, process peak **534.71 MiB**, exported models
**4,047,896 bytes**. Evaluation/uncertainty: **2.41 seconds**. One thread,
Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0. No audio downloads,
encoder inference, live database, global-suite lock or nonpilot Memo inputs.

Commands used: `./experiments/run.py situnes-study freeze`, `prepare`, `predict`,
`evaluate`, and `verify`; original frozen sources remain under `source/`.
To repeat fitting, supply a fresh experiments directory with `--directory` to
each stage. Repeated fits on these exposed data are not independent confirmation.

`./experiments/run.py investigate situnes_813.py`: **3 passed in 4.21s**.
Checks independently establish exact real-cohort/group joins, every outer/inner
boundary and training vocabulary, saved training imputation/scaling, every
exported held-out prediction, native metrics and absence of post-listening inputs.
The initial check caught a 1.28e-15 difference between algebraically equivalent
mean calculations before/after affine scaling. Its tolerance is now 1e-14;
exported model predictions still require exact equality. This verification-only
repair and the optional verification receipt support are explicitly archived in
`postrun-source/`, separate from the prospective freeze. No fitted result changed.
`./experiments/run.py situnes-study verify`: **four valid receipts** (cohort,
predictions, evaluation and post-run verification sources).
