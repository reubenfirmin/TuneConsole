# SiTunes feasibility study (#813)

Freeze this protocol before model fitting. This is a metadata study of recorded
post-listening valence/arousal, not a trial of a TuneConsole recommendation model.
Use publisher revision `aea53feca9ba092406b5b95a3976dba0b44c568f`:
<https://github.com/JiayuLi-997/SiTunes_dataset/tree/aea53feca9ba092406b5b95a3976dba0b44c568f>.
The schema and psychological-satisfaction README are primary interpretation
sources; snapshot both with the experiment. Snapshot the recursive repository
tree to audit audio availability and license notices. Public availability alone
does not establish permission to redistribute the data or recordings.

Include every Stage 2/3 interaction with finite before/after human VA in [-1,1].
Keep Stage 1 in the missingness audit but exclude it from paired analyses because
it has no initial-mood observation. Check unique stage/interaction identifiers,
metadata joins and environment joins; report repeated people, items, user/item
pairs, stage counts, missing fields, scales and candidate-audio coverage. Item
metadata IDs and strict normalized artist/title keys define track groups. Missing
metadata retains an item-ID group. No artist generalization claim is made.

Predict the two **post-mood levels**, independently, with native-scale MSE and
MAE. Fit on (VA+1)/2 and convert predictions back to [-1,1] for reporting. Compare:

- training mean post-mood;
- unchanged initial mood;
- training participant mean, falling back to training global mean for new people;
- initial VA only;
- context only;
- initial VA plus context;
- initial VA plus context and participant identity;
- the preceding inputs plus the supplied music descriptors.

Context is collection stage, recommendation policy, time-of-day category,
weather category, pressure, temperature and humidity. Encode stage/policy as a
joint category: their labels have different meanings between stages. Missing
categories have an explicit token; unseen categories encode as all zero. Learn
category vocabularies only inside each training partition. The music block uses
all 20 numeric item descriptors and categorical general genre. Its `valence` is
a Spotify feature, never the human outcome. Metadata `duration` is item length;
exclude realized interaction duration, ratings, preference, timestamps, post-mood,
interaction/item IDs as predictors. Exclude GPS and wrist signals: their temporal
availability and privacy transformations need a separate audit. Environment
timing is not independently verified, so context comparisons are conditional
observational diagnostics rather than a validated prospective forecasting system.

Use constant plus ridge alphas .1, 1, 10, 100, 1000; select each outcome by inner
pooled MSE with deterministic ties preferring constant, then smaller alpha. Fit
imputation, scaling, category vocabularies and coefficients on training data only.
Use seed 8131101. Three evaluations, with no outer-result model selection:

1. Leave one participant out (30 outer partitions), with three participant-grouped
   inner folds. This estimates transfer to new participants with potentially seen
   tracks.
2. Five track-grouped outer folds and three track-grouped inner folds. This
   estimates transfer to new catalog items with potentially seen participants.
3. Stage 2 training and Stage 3 testing; three participant-grouped inner folds
   within Stage 2. Report participant/track overlap and date ranges. Stage 3 is a
   later, selected subset, not an independent randomized cohort.

Save every partition, inner assignment, feature vocabulary, selected head and
out-of-partition prediction. Do not train a final deployable model. Report paired
MSE differences against unchanged mood, initial-VA regression, and the incremental
context/participant model as applicable. Compute 1,000 conditional bootstrap
draws for participant clusters, track clusters, and two-way pigeonhole resampling
(independently resample participant and track multiplicities, multiply row
weights). These intervals condition on the fitted models and observed collection;
they omit fitting uncertainty and do not establish causation. Include per-person
loss and descriptive pre/post/change summaries. Never use change as a feature;
pre/change correlation is mechanically coupled and cannot show mood regulation.

The existing text-match intersection is only an audio feasibility lead. Compare
available recording lengths and sampled intervals with publisher item lengths;
count before/after rows, people and tracks on those candidates. No source audio,
edition identifiers or listened interval offsets accompany the SiTunes metadata.
Text matches and similar durations cannot verify recording/excerpt alignment.
Do not fetch a broad audio catalog or fit an audio model on this intersection.

Bounds: no new audio downloads, under 2 MiB additional metadata, one CPU thread,
one hour fitting/evaluation, 2 GiB peak process RSS, 64 MiB saved model artifacts.
All work stays in experiments/. Do not access the live database or any nonpilot
Memo outcome, run the global suite, commit or push. Finish with a report, actual
data checks, verified receipts and a retain/research recommendation. VA change
is neither anger relief nor evidence of improved cognitive performance.
