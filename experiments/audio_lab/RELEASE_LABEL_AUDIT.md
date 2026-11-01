# Release label audit #614

30 October 2026. Existing labels support three distinct native tasks. They do
not supply a jointly annotated aggression/power/anger reference or a direct
measurement of how suitable a track is for letting off steam.

The original annotation files reproduce every frozen native target to numerical
precision. Numerical audit: `local/release-614-20261030/audit-1/audit.json`.
The [experiment plan](RELEASE_MAPPING_PLAN.md) keeps these targets fixed.

| Source and native target | Available feature rows | Grouped identities | Protected training rows | High target (at least .65) | Rater support |
| --- | ---: | ---: | ---: | ---: | --- |
| Jamendo explicit aggression | 216 | 104 artist groups | 187 | 22 | 621 votes; 1–3 per track, median 3 |
| Emotify induced power | 400 | 138 artist groups | 255 | 9 | 8,407 responses; 11–59 per excerpt, median 14 |
| Soundtracks anger | 470 | 295 groups | 467 | 29 | Supplied files contain means, without per-item rater counts |

High-target counts describe the full feature cohorts, before the training
exclusions. They are not counts of independent artists or raters. The protected
pools exclude known local and cross-source identities; the main Soundtracks
comparison retains the original 470 common evaluation rows. Its 467-row
protected source pool is a different population used when Soundtracks trains
a model for another source or supplies native held-out predictions.

Jamendo's mean aggression is .1389; 155 tracks have no affirmative aggression
votes and 56 have mixed votes. Five have unanimous affirmative votes. A small
number of votes gives coarse fractions, including .5 when only two votes are
available. Emotify's mean power is .1902, with 74 zero rows and 325 fractional
rows. Repeated-listener identities are not present in the supplied schemas,
so these response totals must not be treated as independent participants.
Soundtracks' mean anger is .1534 after the fixed conversion `(rating - 1) / 8`
from the original 1–9 scale; 211 rows are at the minimum.

Jamendo labels describe whole songs. Emotify labels describe induced emotion
for its rated excerpts; Soundtracks supplies ratings for approximately
15-second excerpts. All three existing feature cohorts use ten seconds of
audio. Those features do not establish how the label varies throughout the
whole rated song or excerpt. Soundtracks Set 1 and Set 2 include related and
repeated stimuli, handled through the existing grouped identities. Set 1 was
the publisher's pilot with fewer ratings; the supplied means do not recover
its per-item rating uncertainty.

## What can be mapped using observed labels

| Fixed reference | Other observed labels available on the same native rows |
| --- | --- |
| Jamendo aggression | Happy, party, relaxed, sad |
| Emotify power | Joyful activation, tension, calmness, sadness |
| Soundtracks anger | Energy, tension, valence, happy, fear, sad, tender |

The diagnostic excludes the reference itself and fits a small linear mapping
inside grouped training folds. Applying the same mapping to observed labels
and audio-predicted labels distinguishes information available in these
co-annotations from losses in this prediction path. It is not an information
upper bound or proof that the three native targets mean the same thing.
Coimbra tag-derived classes and EMMA previews with unverified alignment remain
the exact supplementary controls from #577.

## Audio inventory and the next acquisition boundary

The raw Jamendo file contains 11,565 complete seven-task annotation records.
Only 216 currently have verified local audio and extracted features in this
cohort. Matching the remaining annotations to the publisher's audio checksum
inventory and excluding the 104 known cohort artists leaves **1,072 tracks
from 230 artist IDs**. These are potential matches, not newly downloaded or
verified audio, and not 1,072 independent artists.

The first two unused numerical shards, `01` and `02`, contain just **17 eligible
tracks from 15 artists**. The recorded future acquisition boundary is those
two official shards and at most 1.25 GiB combined transfer, followed by
published per-track checksum verification and full identity exclusion. No
additional audio was acquired for this round. Seventeen tracks would be a
small extension, not a strong resolution of the present precision gap; the
inventory makes that limitation visible before spending the transfer budget.
Even additional Jamendo rows would primarily test aggression generalization,
not independently confirm transfer to anger or establish cathartic suitability.

A supplementary inventory after the freeze, before model fitting, orders the
unused shards by eligible row count, breaking ties by shard number. It uses
the same complete-annotation, published-checksum and known-artist exclusions;
neither rating values nor model scores select shards. Shards `70` and `63`
would supply 21 and 20 potential tracks, **41 tracks from 34 artists combined**.
Those are a more efficient option for a future two-shard plan with the same
1.25 GiB ceiling. They still provide only a small native-aggression extension.
This inventory refinement changes no input, candidate, acceptance rule or
acquisition in the frozen experiment; any later acquisition needs its own
prefit identity list and protocol.

New Jamendo-derived artifacts remain research-only under the source terms
pinned in the experiment; they inherit no previous export's acceptance.
All 216 Jamendo and 470 Soundtracks rows lack artist names, although Jamendo
artist IDs and Soundtracks groups remain available. Unknown cross-corpus
aliases and encoder-training overlap remain unresolved. The refreshed local
snapshot contains 84 previously inspected identities and supplies supplementary
positive labels only; it is not a new confirmation population.
