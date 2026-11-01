# Uplift labels and coverage, #618

30 October 2026. The [frozen protocol](UPLIFT_CONSENSUS_PLAN.md) uses existing
human labels to learn separate native predictors, then combines their Uplift
scores. No supplied dataset directly labels whether listening to a song will
improve someone's mood. Valence, happiness and joyful activation remain distinct
constructs; their numerical scales are not interchangeable probabilities.

The frozen inventory is `local/uplift-618-20261030/round-1/inputs/audit.json`.
All 3,998 source rows are usable for native development. Related rows share
joint identity components, so this is not a count of independent recordings.

| Source | Rows / groups | Native Uplift target | Meaning, scale and coverage |
| --- | ---: | --- | --- |
| DEAM | 1,800 / 1,796 | Valence | Static listener mean on supplied audio; original 1–9 converted with `(x-1)/8`. One ten-second feature excerpt. |
| PMEmo | 766 / 266 | Valence | Normalized positivity of the supplied chorus, on 0–1. One ten-second feature excerpt; not whole-song supervision. |
| Soundtracks | 470 / 295 | Separate valence and happiness | Excerpt ratings, original 1–9 converted with `(x-1)/8`. Fixed evaluation reference is their mean. Roughly fifteen-second rated excerpts, ten-second features. |
| Jamendo human | 216 / 104 | Happy vote fraction | Explicit affirmative/negative whole-song votes, 0–1. One ten-second feature excerpt; missing uploader tags are not negative labels. |
| Emotify | 400 / 138 | Joyful activation | Fraction of responses reporting this induced emotion, 0–1. Includes activation, so does not directly cover gentle positivity. Ten-second feature excerpt of the rated audio. |
| ManyMusic natural | 188 / 166 | Valence | Perceived emotion of an exact thirty-second window. Signed −1 to 1 converted with `(x+1)/2`; three complete ten-second features. |
| ManyMusic generated | 95 / 88 | Valence | Same task, reported separately and grouped by prompt. |
| Memo pilot | 63 / 62 | Valence | Expert trajectory means over the complete 15–75-second interval. Signed −1 to 1 converted with `(x+1)/2`; six aligned ten-second features and separate matched targets for shorter coverage. |

Joint components include supplied recording/artist/group IDs, audio hashes and
conservative equal normalized titles. No additional component intersects the
84-row local snapshot. The historical common mask retains 171 natural ManyMusic
rows for comparisons with prior controls; all 188 can train, subject to complete
group exclusion in each validation fit. Unknown aliases and encoder pretraining
overlap remain unresolved. Artist names are absent for 1,751 DEAM, all 470
Soundtracks, all 216 human Jamendo, all 95 generated and all 63 Memo rows.
Jamendo artist IDs and Soundtracks groups still provide grouping information.

## Rater support and disagreement

Jamendo's 216 happy targets reproduce the original human-annotation file exactly.
They contain 621 votes, one to three per track, median three: 84 zero targets,
96 mixed-vote targets and 36 unanimously affirmative targets. Mean happy is
0.3881; 81 tracks reach 0.65. The raw TSV SHA256 is
`466cbb888fe906f3db22b9e6f63b1694e6a38a8613fb67d9d52149e1cc6decd3`, under the
existing `datasets/mtg-jamendo/` pinned source. This supplements the frozen
metadata, which omits the per-task rater-count array.

Emotify has 11–59 responses per excerpt (median 14; 8,407 total). Joyful
activation averages 0.2619; 66 targets are zero and 26 reach 0.65. Response
counts are not counts of independent participants. The other native emotions
are present on the same excerpts, but there is no corresponding gentle-positive
or valence label that can establish equivalence with the VA sources.

The selected natural ManyMusic windows have 230 listener/window observations,
one to four per window; 151 of 188 have only one listener. The generated windows
have 948 observations, four to 27 per window, median nine. Retained signed-scale
valence standard deviations average 0.0502 and 0.3745 respectively. A zero
standard deviation for a single listener says nothing about agreement. The
existing import averages repeated responses within listener/window first;
neither response count nor multiple windows increases a recording's weight.

The frozen DEAM, PMEmo and Soundtracks metadata does not retain per-item rater
uncertainty, and Memo supplies averaged trajectories. Treat that uncertainty as
unavailable, rather than inventing precision. Soundtracks mean normalized
valence is 0.4225 and mean happiness is 0.1724. This difference alone is a warning
against interpreting the two quantities as the same calibrated score.

## Available supervision and diagnostic boundary

Soundtracks co-annotates valence, happiness, energy, tension, anger, fear, sadness
and tenderness. The primary diagnostic predicts mean(happiness, valence) using
the other six attributes, excluding both reference-defining columns. A separate
diagnostic predicts happiness from valence alone. Comparing observed attributes
with cross-fitted audio predictions probes where this particular mapping loses
information; it is neither an information upper bound nor proof of product
suitability. There are no fabricated cross-source co-annotations.

Fixed 0.35/0.65 contrast slices and their exact IDs are reported with the results.
VA low-arousal/positive slices are proxies; joyful activation does not directly
measure gentle positivity. Memo supports aligned one/three/six-excerpt targets.
ManyMusic's whole-window rating cannot validate individual excerpt scores.

## Supplied versus usable data

Reuse the [dataset inventory](DATASET_PLAN.md) and [original campaign](INDIVIDUAL_EXPERIMENT_RESULTS.md).
DEAM supplies 1,802 audio/annotation pairs: `2048` fails the complete ten-second
boundary and `2031` was excluded by the inherited exposure policy, leaving
1,800. PMEmo supplies 767 choruses; inherited exclusion `487` leaves 766.
All 470 Soundtracks and 400 Emotify records are retained. Jamendo has 11,565
complete seven-task annotation records, with 216 verified audio/feature matches
from the four acquired shards. This round acquires no additional audio.

ManyMusic supplies 1,695 rated windows and 283 matched acquired recordings;
the identity-selected window per recording yields 188 natural and 95 generated
rows. Memo has 64 acquired pilot files, with 63 complete labeled intervals;
its full 2,496-recording collection is not acquired here. More annotations do
not create more usable independent songs without compatible audio and joins.

The refreshed read-only local snapshot has 84 usable labeled identities and
23 Uplift positives, with no new identities since #570. Five earlier answers
to the same song, four old-vocabulary answers, five empty/cleared answers,
two invalid/missing embeddings and three unavailable captures are excluded.
These counts describe export exclusions, not additional negative Uplift labels.
Local first/all-capture scores are supplementary and never select weights.

Source terms remain those recorded in the frozen plan and original inventories,
including Jamendo noncommercial research terms and ManyMusic's noncommercial
share-alike terms plus individual natural-track licences. The artifacts are
research exports; this investigation does not establish distribution clearance.
