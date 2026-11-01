# Focus label and identity audit (#615)

30 October 2026. Focus remains the equal mean of original CAL500 Reading and
Studying listener fractions. External emotion labels help predict those
fractions; they do not supply additional direct Focus judgments.

## Direct activity supervision

The retained CAL500 audio inventory contains 494 songs and 1,677 original
responses. There are 2–14 listeners per song, median 3. Reading has at least one
positive response on 137 songs, Studying on 163; 214 have either and 86 have
both. Mean fractions are 0.09418 and 0.11865 respectively. The common comparison
still contains the same 450 known-product-training-disjoint songs used in #576.

Repeated split-half response partitions give median listener correlations of
0.0538 for Reading and 0.0802 for Studying. Their 10th–90th percentile ranges
are [0.0173, 0.0854] and [0.0331, 0.1202]. These describe unstable agreement in
sparse, small response groups; they are not model accuracy ceilings or evidence
that the intended activities lack meaning. The labels describe whole songs;
the retained audio features describe one ten-second excerpt per song.

All ten selected ordinal attributes are present in every original response:
energy, changing energy, heavy beat, tempo, calming, arousing, pleasant, tender,
powerful and mellow. Each raw value is 1–5, mapped to `(value - 1) / 4` and then
averaged across listeners. The original README describes three emotion levels,
which conflicts with these raw files. This discrepancy remains explicit.
Median split-half correlations range from 0.191 for changing energy to 0.595
for tenderness. The observed-attribute diagnostic measures associations among
co-annotated variables; it cannot establish a causal effect on concentration.

Vocal coverage uses the original male/female lead, backing-vocal and choir
fields. Any known Present/Prominent value establishes vocal presence; all None
establish absence. Other unresolved responses remain unknown. There are 1,668
resolved response-level observations and 9 unresolved ones. These annotations
support descriptive contrasts only; vocal presence is neither a model input nor
an exclusion rule. No distraction or instrumental-suitability labels are
invented from them.

## Native attribute sources

| Source | Recording rows | Retained chunks | Fitted native labels |
|---|---:|---:|---|
| CAL500 | 494 | 494 | Ten ordinal attributes above |
| DEAM | 1,800 | 1,800 | Arousal, valence |
| PMEmo | 766 | 766 | Arousal, valence |
| ManyMusic natural | 188 | 564 | Arousal, valence |
| ManyMusic generated | 95 | 285 | Arousal, valence |
| Memo pilot | 63 | 378 | Arousal, valence |
| Soundtracks | 470 | 470 | Energy, tension, tenderness |
| Emotify | 400 | 400 | Calmness, tension, power |

These total 4,276 source rows across seven source families, including CAL500.
Rows are not necessarily unique recordings across sources. Twenty-six separate
native heads become eighteen mapping inputs: ten CAL500 predictions, two
family-balanced valence/arousal predictions, three Soundtracks predictions and
three Emotify predictions. Natural/generated ManyMusic split one family's
consensus weight equally. Only CAL500 supplies the final activity supervision.

The different annotation units remain separate: CAL500 whole songs, DEAM static
supplied-audio means, PMEmo choruses, ManyMusic perceived emotion over recorded
30-second windows, Memo expert trajectories over the complete 15–75-second
interval, Soundtracks excerpts and Emotify excerpt-induced emotion. The model
does not interpret them as interchangeable Focus labels.

## Identity and local evidence

Global components combine known artist, recording, audio, title and declared
group relationships across every source and the refreshed local inventory.
Five resulting CAL500 components span old #576 folds. New five-fold partitions
keep these components intact, and every held-out component is excluded from
all native sources throughout outer selection, inner selection and mapping
cross-fitting. The exact #576 historical/browser selection procedures are
refitted on those partitions for comparison. Archived predictions remain
legacy diagnostics with weaker isolation.

CAL500 has 483 known components. Identity coverage is incomplete elsewhere:
artist names are missing on 1,751 DEAM rows, 470 Soundtracks rows, all 95
generated ManyMusic rows and all 63 Memo rows. Other available declared groups
and identities still apply. Unknown identities and encoder-training exposure
prevent a claim of perfect independence.

The refreshed read-only local snapshot has 84 usable songs, including 18
explicit Focus positives, and 232 retained captures. There are no new usable
identities or changed labels since #570. No external component intersects these
known local components. Missing local Focus judgments remain unknown; local
scores and first/three/all-capture changes are supplementary replay evidence.
The full audit records excluded duplicate, obsolete, empty and unusable answers.

The earlier [reference audit](FOCUS_REFERENCE_RESULTS.md) remains applicable:
Moods and activities has 209 candidate text joins but zero verified
recording/window matches; HelpStudy outcomes describe episodes rather than
individual tracks. Neither adds direct evaluation rows. This round does not
acquire audio or convert these broader outcomes into Reading/Studying truth.

## Provenance

The frozen audit is `local/focus-615-20261030/round-1/inputs/audit.json`.
Every original response file, native feature input, prior control, source-code
dependency and local snapshot is hashed in `plan.json`, SHA256
`5af390b1c1173e4d2978f6ca34835b955a4d81a6206c71965fb855f2d1545486`.
The original source terms, including ManyMusic NC/SA constraints and Memo's
CC BY 4.0 attribution, remain attached to the earlier acquisitions. These are
research artifacts; this audit does not establish distribution permission.
