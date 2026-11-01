# Energize supervision and mapping coverage (#616)

30 October 2026. This inventory accompanies the
[frozen experiment plan](ENERGIZE_CONSENSUS_PLAN.md). The available labels cover
perceived arousal, perceived energy, induced power/joy and exercise suitability.
Those are different tasks. Their native predictors remain separate before the
declared mappings; positive valence is not an Energize requirement.

Frozen inventory: `local/energize-616-20261030/round-1/inputs/audit.json`, SHA256
`bc92fb14d376a0dc68f3f4cfa5d1857e0d0faf064350d690d122bf2c002005e4`.
There are 4,276 usable source rows. Shared identities and artists mean these
are not 4,276 independent musical examples.

| Source | Rows / groups | Native target | Meaning and alignment |
| --- | ---: | --- | --- |
| DEAM | 1,800 / 1,796 | Arousal | Static listener mean, original 1–9 normalized with `(x-1)/8`; one ten-second feature excerpt. |
| PMEmo | 766 / 266 | Arousal | Normalized 0–1 chorus arousal; one ten-second feature excerpt. |
| Soundtracks | 470 / 295 | Energy | Excerpt rating, original 1–9 normalized with `(x-1)/8`; ten-second features from the rated excerpt. |
| Emotify | 400 / 137 | Power and joyful activation | Separate induced-emotion response fractions, 0–1; ten-second feature excerpt. |
| ManyMusic natural | 188 / 166 | Arousal | Perceived emotion of a thirty-second window, signed −1 to 1 mapped with `(x+1)/2`; three aligned chunks. |
| ManyMusic generated | 95 / 88 | Arousal | Same task, separated from natural music and grouped by generation prompt. |
| Memo pilot | 63 / 62 | Arousal | Expert trajectories averaged over the complete 15–75-second interval; six aligned chunks and matching shorter-window targets. |
| CAL500 | 494 / 483 | Exercising | Original unfiltered Yes-checkbox fraction for a whole song; one ten-second feature excerpt. |

The unchanged common comparison masks retain 171 natural ManyMusic rows and
428 CAL500 rows. All 188/494 may train subject to complete validation-component
exclusion. Across the five primary reference populations this gives 1,227
comparison rows. Browser and historical preparations describe the same rows,
not additional songs.

Joint groups connect known recording/artist/group IDs, audio hashes and
conservative normalized-title matches. No additional source component matches
the refreshed local snapshot. Missing artist names remain substantial: 1,751
DEAM rows and every Soundtracks, generated ManyMusic and Memo row. Soundtracks
film IDs and generated prompt IDs still provide grouping. Unknown aliases and
encoder pretraining exposure remain unresolved.

## Original exercise judgments and co-annotations

The 494 CAL500 songs have **1,677 responses**, with 2–14 responses per song and
median three. Reconstructing the original Exercising checkboxes reproduces the
frozen activity targets exactly. Omitted checkboxes mean unselected within this
source's original aggregation; this rule is not applied to local vibe labels.
There are **106 songs with any exercise vote**, 388 with none, and only 13 with
exercise fraction at least 0.65. Mean exercise fraction is 0.0770.

The release's published activity matrix discards single-Yes judgments. This
experiment retains the previously audited unfiltered task, preserving those
votes rather than silently substituting the published benchmark.

Seven attributes are available from the same original response files: High
Energy, Heavy Beat, Fast Tempo, Very Danceable, Powerful/Strong,
Cheerful/Festive and Arousing/Awakening. All selected raw responses are integers
1–5 without missing entries. The diagnostic transforms them with `(value-1)/4`
and averages listeners. The supplied README describes emotions as three-level
ratings, which conflicts with the five-level raw fields. The raw encoding is
preserved explicitly; this normalization does not prove interval-scale or
cross-dataset equivalence. No Usage attribute enters the mapping inputs.

Repeated random splits of each song's available listeners produce these median
correlations between the two halves:

| Label | Split-half correlation |
| --- | ---: |
| High Energy | 0.594 |
| Heavy Beat | 0.456 |
| Fast Tempo | 0.584 |
| Very Danceable | 0.531 |
| Powerful/Strong | 0.283 |
| Cheerful/Festive | 0.523 |
| Arousing/Awakening | 0.410 |
| Exercising | **0.116** |

This is descriptive agreement on sparse existing responses, not an accuracy
ceiling. Exercising's split 10th–90th percentile range is 0.068–0.160. The
semantic diagnostic tests whether these observed attributes explain exercise
judgments, then whether cross-fitted audio predictions retain that information.
Same-rater associations and the whole-song/short-excerpt mismatch limit the
interpretation; they do not establish a causal exercise benefit.

## Other rater support and coverage

Emotify has 8,407 responses, 11–59 per excerpt, median 14. Power and joy remain
separate native outputs. Their equal mean averages 0.2261; only three excerpts
reach 0.65 on that combined reference. Response counts do not imply that every
response comes from a different person.

Natural ManyMusic has 230 listener/window observations, one to four per window;
151 of 188 windows have one listener. Generated music has 948 observations,
four to 27 per window, median nine. Mean retained signed-scale arousal standard
deviation is 0.0622 for natural and 0.4040 for generated windows. A zero standard
deviation from one listener is not agreement evidence. Repeated responses are
averaged within listener/window before forming targets.

Per-item rater uncertainty is unavailable in the retained DEAM, PMEmo and
Soundtracks metadata; Memo supplies averaged expert trajectories. Do not infer
precision from missing uncertainty. Native mean reference levels differ:
DEAM 0.4769, PMEmo 0.6221, Soundtracks 0.4251, natural ManyMusic 0.4795,
generated ManyMusic 0.4435 and Memo 0.4986. These scales are not interchangeable
calibrated suitability probabilities.

Observed-label slices use the frozen 0.35/0.65 thresholds. VA can distinguish
energetic-positive from energetic-negative examples but cannot label power or
exercise suitability. CAL500 provides the direct power/tempo contrast;
Soundtracks anger with lower energy is explicitly a proxy. Memo alone supplies
the aligned trajectories needed to score shorter feature windows against
matching shorter reference windows.

## Reuse and local evidence

No new audio was acquired. Reuse the supplied/usable counts and exclusions in
[the dataset inventory](DATASET_PLAN.md) and the completed
[individual campaign](INDIVIDUAL_EXPERIMENT_RESULTS.md). Existing acquisition
limits include 283 selected ManyMusic recordings and 63 complete Memo pilot
intervals; this round does not imply coverage of their full releases.

The refreshed local export contains **84 usable songs, 232 captures and 40
Energize positives**, with no new identities or changed labels since #570.
Receipt SHA256:
`dd804e7fea3db06c9f2691df34c77589527f268520f45047952b00beaf593abc`.
The export uses read-only SQL and copied captures. Local first/three/all scores
are supplementary; unselected vibes stay unknown and never select a model.

Retain original source notices and the source-terms status recorded in the
frozen plan. These are research artifacts, with no new distribution clearance.
