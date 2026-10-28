# Offline external-data experiments

Related issue: #507. All artifacts stay in `local/external-20261028/`, which is
ignored by Git. The runner never opens an application database or writes an
application model. The six vibe meanings and developer annotations are unchanged.

Run the complete local-audio sequence from the repository root:

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_experiment suite --native
```

This command requires no network access or further downloads. Its native Node
dependency is already installed under ignored `local/node/`, separate from the
product dependencies. One low-priority extraction worker processes one recording
at a time with one-thread native kernels and FFmpeg. Experiments do not acquire
or bypass the pytest lock. A separate experiment lock prevents duplicate suites.

The sequence is Soundtracks Set 2, CAL500, Emotify, PMEmo, then the alternate
Soundtracks Set 1+2 experiment. Features are cached by source-audio hash,
extractor/model provenance, backend, decoder version, and excerpt position.
Rerunning resumes from cached successes. Individual corpus failures are recorded
and do not stop subsequent corpora.

## Evidence and progress

- `suite-status.json`: current corpus, training/transfer outcomes, and protected
  active-model hashes.
- `<dataset>.status.json`: extraction count, errors, elapsed time, and timestamp.
- `<dataset>.index.json`: exact audio/annotation joins and grouping identities.
- `<dataset>.features.json`: successful extractions, errors, and provenance.
- `<dataset>.report.md` and `.report.json`: native-label comparisons.
- `<dataset>.oof.csv` and `.splits.json`: auditable held-out predictions and folds.
- `cal500.ranking.md` and `.json`: activity average precision, AUC, and human-vote
  weight lift among the ten highest-ranked tracks in each held-out fold.
- `annotation-audit.json`: raw response counts, descriptive split-half stability,
  and reconstruction of CAL500 activity aggregation from individual annotations.
- `emotify.genre-check.md` and `.json`: genre-only baseline and prediction
  correlations within each genre, computed from the same held-out folds.
- `models/`: experimental regression coefficients, in a research-only format.
- `transfer-<datasets>.report.json`: diagnostic transfer to the existing gold
  snapshot; no prediction-only examples are promoted to human labels.
- `transfer-status.json`: progress of the transfer recovery worker. The initial
  suite imported the transfer module before a missing-embedding fix; its original
  error remains in `suite-status.json` as history, while corrected comparisons
  are produced by `.venv/bin/python -m experiments.audio_lab_tools.external_transfer --watch`.

When output is redirected to `suite.log`, it remains a readable progress log.
The status files update after each ten extracted examples and each training
stage. A complete suite takes roughly two hours on the measured one-worker
throughput; the first useful corpus comparison is produced much earlier.

## What is compared

Decode the center ten seconds of each supplied recording with FFmpeg. Feed the
same excerpt into the unmodified Radio research extractor: 27 audio attributes,
the 200-value frozen MusiCNN embedding, and six pretrained mood outputs. The
research Node harness uses native TensorFlow kernels. Its initial parity check
against TFJS CPU found identical attributes, a maximum embedding difference of
0.00000644, and a maximum mood-score difference of 0.00000073. FFmpeg resampling
differs from the browser's audio decoder; browser bitwise equivalence is not
claimed.

Fit separate native-label regression tasks for each dataset. Compare attributes,
embeddings, pretrained moods, attributes plus embeddings, and all three. Keep
each dataset's meanings and scales separate. Normalize native labels to 0–1;
use published soft annotation weights as targets, not automatically hard classes.

Nested validation uses five outer grouped folds and three inner grouped folds.
Imputation, standardization, and regularization selection use training folds
only. Baselines predict the training mean and training median. Reports include
MAE, squared error, per-target correlations, and paired group-bootstrap intervals.
CAL500's mood/activity targets choose regularization; its other tags are retained
for inspection. Sparse-label results must also be assessed against the median
baseline and correlations, not solely the training mean.

Grouping keeps Soundtracks films and linked excerpts together where identities
are supplied, CAL500 artists together, Emotify recordings together, and PMEmo
primary artists together. Identical encoded audio also shares a group. Set 1
has incomplete film identities, Emotify lacks artist identities, and PMEmo guest
artists can overlap across groups; these limitations are recorded in reports.

After each independent corpus finishes, use its embedding-based heads as frozen
quality features for the six existing vibes. External examples matching a gold
artist or title are excluded before fitting these transfer heads. Check exact
embedding/model-weight provenance against the saved gold snapshot. Compare
embedding-only learning with external-quality features and their combination,
using nested artist-group validation on the 55 developer-labeled songs with
embeddings. One of the 56 gold songs has no embedding in the saved snapshot;
it is explicitly excluded from this comparison and its original label remains
unchanged. The 63
prediction-labeled songs remain a separate silver set and are excluded from this
diagnostic. This small, previously examined gold snapshot cannot establish
product-wide quality.

## First completed comparison

Soundtracks Set 2: all 110 excerpts extracted, 37 validation groups, no extraction
errors. Mean held-out absolute error across eight normalized emotion ratings:

| Features | MAE |
| --- | ---: |
| Training mean | 0.1890 |
| Training median | 0.1807 |
| Attributes | 0.1616 |
| Embeddings | 0.1121 |
| Pretrained mood outputs, fitted to this corpus | 0.1300 |
| Attributes and embeddings | 0.1133 |
| Attributes, embeddings, and mood outputs | 0.1127 |

These results support native-emotion learning from the supplied annotations.
The separate 55-song vibe diagnostic found no improvement from adding the
Soundtracks heads to embeddings: both achieved 61.8% top-choice acceptability,
with slightly worse acceptable-label log loss for the combined features. CAL500
adds activity judgments and must be assessed separately. Full evidence is saved
in `local/external-20261028/soundtracks-set2.report.json` and
`transfer-soundtracks-set2.report.json`.

CAL500 has 496 usable recordings. Four supplied recordings are shorter than ten
seconds, and two annotation rows lack supplied audio; exact exclusions are saved
in the join audit and feature manifest. Activity tags are sparse: the median
baseline beats the fitted models on aggregate absolute error. Ranking provides a
different useful check. For embeddings, party average precision is 0.390 against
0.125 prevalence; going to sleep is 0.285 against 0.111; exercising is 0.142
against 0.044. Attributes reach 0.347 for going to sleep and 0.220 for exercising.
Work and studying remain weak. These exploratory activity results do not establish
six-vibe improvement. Adding Soundtracks and CAL500 heads to embeddings gets one
more gold song right (63.6% versus 61.8%), while acceptable-label log loss worsens
from 1.040 to 1.069.

The raw-annotation audit reconstructs all 7,530 supplied CAL500 activity targets:
the published weight is the Yes fraction when at least two listeners select the
activity, otherwise zero; binary activity targets indicate nonzero weight. This
differs from the README's stated 80 percent consensus rule. A separate
`cal500-raw-activity` experiment retains the unfiltered Yes fractions. Omitted
checkboxes are interpreted as unselected. Its target definition differs from the
published benchmark, which remains intact. `annotation-audit.json` also records
descriptive listener split-half stability for CAL500 and Emotify.

Emotify completed all 400 excerpts without extraction errors. Embedding MAE is
0.1103 versus 0.1444 for the training mean and 0.1372 for a training-genre-mean
baseline. Within-genre mean target correlations range from 0.381 to 0.573;
calmness, power, and joyful activation correlations across the corpus are 0.617,
0.588, and 0.661. Artist identities are unavailable, so these are recording-held-out
results. Adding its heads to Soundtracks and CAL500 still gives 63.6% versus 61.8%
top-choice acceptability on the small vibe diagnostic, with worse log loss
(1.064 versus 1.040). Native emotion learning is useful evidence; convincing
six-vibe improvement remains unproven.

PMEmo completed all 767 supplied choruses without extraction errors, across
398 primary-artist groups. Embedding MAE is 0.0844 versus 0.1443 for the training
mean; attributes plus embeddings reach 0.0840. These remain native
arousal/valence results, separate from the small six-vibe diagnostic.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_annotations
.venv/bin/python -m experiments.audio_lab_tools.external_ranking
.venv/bin/python -m experiments.audio_lab_tools.external_strata --wait
```

## Unrated-audio consistency experiment

The consistency run waits for the native suite to finish, then selects 160 PMEmo
entries by a seeded audio-content hash. A subsequent audit found 159 unique
recordings: one repeated entry was confined to the training partition. The
original 128 training entries therefore contain 127 unique recordings; all 32
held-out recordings are distinct and disjoint from training. Preserve the original
run, and repeat it with the duplicate removed. Selection does not use emotion labels,
vibe labels, or prediction scores. Exclude known artist, title, and identical-audio
overlaps with CAL500 and all gold songs. Preserve this selection and all exclusions
in `local/external-20261028/unlabeled-consistency/pool.json`.

Create one real audio augmentation of the same center excerpt: attenuation
between 3 and 9 dB plus noise at 35 dB signal-to-noise ratio. Collect its frozen
embedding alongside the existing clean embedding. No distant song sections are
forced to agree, and no prediction becomes a training label.

Use 128 pairs for learning and reserve 32 separate recordings for robustness
measurement. Reserved pairs never enter fitting or parameter selection. Report
prediction changes alongside prediction variability to expose a constant-output
shortcut; these measurements supplement the labeled held-out quality comparisons.

Compare ordinary ridge training with a consistency-regularized version. The new
term penalizes squared differences in predictions across these unlabeled audio
pairs; existing external human annotations remain the supervised target. Tune
its strength, including zero, inside the existing nested grouped folds. Compare
Soundtracks emotion prediction, CAL500 mood/activity prediction, and the preserved
55-song vibe diagnostic. Export research coefficients and held-out predictions
without activating a candidate.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_consistency --wait-for-suite --pairs 128
.venv/bin/python -m experiments.audio_lab_tools.external_results --watch
```

The combined results are written to `local/external-20261028/RESULTS.md`.
The augmentation worker passed a real-audio pilot. With consistency strength
zero, its predictions matched ordinary ridge to a maximum absolute difference
of 0.000000000000002. Quality improvement from unrated audio remains unproven
until the held-out comparisons complete. The original completed comparison
improved Soundtracks emotion MAE from 0.1121 to 0.1082, left CAL500 effectively
unchanged, and worsened the gold vibe log loss. That is a limited native-task
gain, not established six-vibe improvement. The duplicate-free repeat is queued
with separate artifacts.

## Dataset-expert consensus

Each completed native dataset supplies a separate expert. Calibrate each expert's
native qualities to the six preserved vibes inside artist-separated training
folds. Compare equal probability pooling and learned convex pooling, both with
and without the embedding-only expert. Learn mixture weights from predictions
that exclude the corresponding song and artist; select each expert's ridge
strength inside that meta-training split. The entropy prior toward equal weights
is fixed before evaluation. No prediction becomes a human annotation.

The consensus search also compares every subset of the available experts, with
equal and learned pooling for every multi-expert subset. Four dataset experts
plus embeddings give 31 nonempty subsets (26 consensus subsets); VGMIDI expands
this to 63 (57 consensus subsets). The alternate overlapping Soundtracks-all
corpus is not counted as another independent expert.

`selected_combination` chooses a multi-expert subset and pooling policy within
each outer training fold. `selected_candidate` also permits a single expert.
Both use acceptable-label log loss on cross-fitted inner expert predictions;
those inner labels fit the pooling weights as well as rank the candidates.
Only the outer fold estimates the complete selection procedure. Fixed-subset
outer scores are also retained, but choosing their apparent winner afterward
does not independently validate it. Save the full search, fold selections, and
weights in the report and exported research model.

The first three-dataset comparison gives 63.6% acceptable top choices and 0.979
log loss for learned external consensus, against 61.8% and 1.040 for embeddings.
Emotify alone gives 69.1% and 0.928. This is the same previously examined 55-song
diagnostic, so selecting the best result afterward cannot establish a final model.
Agreement checks are inconclusive; these experts share embeddings and can share
errors. Keep per-fold weights, individual expert scores, error correlations, and
disagreement diagnostics in `consensus-*.report.json`, with research coefficients
under `models/`.

The four-dataset search (Soundtracks Set 2, CAL500, Emotify, PMEmo, plus the
embedding expert) completed all 31 nonempty subsets and 57 fixed candidates:
five individual experts and two pooling policies for each of 26 multi-expert
subsets. No fixed combination beats Emotify alone on either acceptable top
choices (69.1%) or acceptable-label log loss (0.928). The highest fixed-combination
top-choice score is 67.3%; its best log loss is 0.952. Choosing the combination
inside each training fold gives 63.6% and 1.000; allowing a single expert to win
the same search gives 63.6% and 1.007. These are exploratory results on the
previously examined 55 songs, not a final model selection.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_consensus --watch
```

## Additional supplied-audio queue

VGMIDI's author repository contains 99 rendered WAV files, now downloaded into
ignored `local/datasets/vgmidi/source.git`. Seventy-six recordings passed the
join checks. The follow-on runner verifies
original annotation titles, franchise identities, and durations before pairing
the WAVs with complete listener annotations. It uses equal-listener whole-piece
means from the original time-varying annotations, a separate weak-label task from
the published phrase classes. It does not render new MIDI or substitute game
soundtrack recordings.

The VGMIDI extraction waits until the initial suite and unlabeled consistency
finish, preserving one extraction worker. It then trains native-label models,
updates transfer comparisons and consensus, and saves its join audit and errors.
Composer identities are unavailable; validation groups related game franchises.
This supplied-audio subset is not the complete 200-piece collection.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_vgmidi
```

`additional-datasets-status.json` records acquisition, verified rows, queue state,
and completion or failure. The combined results watcher follows this extra queue.

## Expanded campaign and follow-up trials

The current sequential campaign adds 216 verified Jamendo human-annotated tracks,
903 Coimbra MIREX-like clips, 900 4Q clips, and 1,802 DEAM recordings. The original
and expanded Soundtracks datasets replace one another in mixtures. After native
comparisons it runs all expert subsets, with alternate native learners and vibe
calibrators. `campaign-status.json` and `campaign-expanded.log` record progress.

```sh
.venv/bin/python -m experiments.audio_lab_tools.external_campaign
.venv/bin/python -m experiments.audio_lab_tools.external_followups
.venv/bin/python -m experiments.audio_lab_tools.external_results --watch
```

The follow-up runner waits for that extraction campaign, then reuses its cached
features for original AMG1608 and NTWICM song-identity intersections, native
classification, time-aligned DEAM/PMEmo supervision, unlabeled PCA/whitening,
duplicate-free consistency, and a small shared nonlinear representation. Every
trial retains its own report, predictions, provenance, and research model. Failed
stages are recorded without blocking unrelated remaining trials.

The product ranks songs within a selected vibe. A separate ranking experiment
therefore fits six independent suitability scores and tests every expert subset,
including learned weights specific to each vibe. Selection uses nested
artist-separated class-balanced binary log loss; reports retain per-vibe average
precision and AUC. Unselected alternatives in the old gold snapshot are not
explicit human rejections, and the scores are not population probabilities.

The first ranking pilot uses Emotify and PMEmo plus embeddings. Its held-out macro
average precision is 0.586 for Emotify, 0.446 for embeddings, and 0.553 for the
inner-selected combination. This is promising diagnostic evidence for an Emotify
head; it is still the same small, previously examined developer snapshot. The
larger ranking search and the shared-representation comparison remain queued.
Reports are `vibe-ranking-*.report.json`; exports are `models/vibe-ranking-*.json`.
