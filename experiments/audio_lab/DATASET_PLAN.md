# External datasets for the vibe model

Related issue: #507. Acquisition status updated on 28 October 2026.

## Product checkpoint and next experiments

The user requested a stopping point on 28 October. Pause the broad campaign;
retain downloaded audio, extracted features, and unfinished trials. Emotify's
linear native-emotion expert with linear six-vibe calibration is the chosen
checkpoint. See [model evidence and integration](MODEL_CHECKPOINT.md).

Provider audio metadata supplies initial vibe estimates. Normal playback
sampling then adds actual measurements, which feed the learned model and seed
bounded artist/genre predictions for unmeasured recordings. Only the developer
vibe-rating UI is opt-in; ordinary collection must not depend on that switch.
Predicted recordings never become independent measurement votes. Heart/skull
continues to mean song preference, and regular users never supply vibe labels.

Resume research in this order, retaining the checkpoint as the comparator:

| Priority | Data | Next use |
| --- | --- | --- |
| 1 | Emotify, 400 supplied excerpts | Compare the existing center window with early/center/late means, and unweighted training with listener-count weighting. Keep its nine native emotions and unchanged six-vibe meanings. This directly tests improvements to the shipped checkpoint. |
| 2 | DEAM, 1,802 supplied recordings; PMEmo, 767 choruses | Resume DEAM extraction from its cache (last checkpoint: 50 complete, no errors). Train native static activation/positivity, then compare time-aligned windows. Test cross-corpus transfer in both directions without selecting on the developer vibe set. |
| 3 | CAL500, 496 usable recordings | Reuse cached audio features to test original listener fractions for studying, reading, working, exercise, and driving. Focus is currently weak; activity evidence is more direct than inferring it from calmness alone. |
| 4 | EMMA, 333 downloaded publisher previews | Train nine GEMS dimensions as an additional expert. Treat the 15-second previews as weak supervision because alignment with the originally rated excerpts is unverified. Compare alone, with Emotify, and in nested consensus. |
| 5 | MERGE Audio Complete, 3,554 supplied clips | Resume the partial official download, verify its checksum, and train manually checked quadrants. Compare tag-derived continuous coordinates separately. Replace overlapping 4Q in mixtures, never give both independent votes. |
| 6 | AMG1608 and NTWICM annotations | Use strict intersections with already acquired audio first: roughly 400 AMG1608 matches and 55 distinct NTWICM matches. Keep song/excerpt mismatch explicit. Test their native activation/positivity and transfer to PMEmo/DEAM before collecting more recordings. |
| 7 | Soundtracks, 4Q, Coimbra, VGMIDI, Jamendo | Reuse completed features for source ablations, learner comparisons, and every nonempty consensus subset, including pairs. Use overlapping corpus versions as alternatives. Jamendo remains subject to its stated noncommercial research terms. |
| 8 | Unrated audio pairs and all ready native corpora | Compare a duplicate-free consistency objective, unlabeled PCA/whitening, and shared representations with separate native tasks. Withhold native labels for the unlabeled pool and remove its robustness holdout from all matched training tasks. Never promote model agreement or playback duration to human truth. |
| Later | Remaining catalogue releases and unmatched songs | Follow the dataset-specific roles below. Prioritize usable supplied audio; YouTube identification and sampling comes after ready audio. TROMPA-MER, FME-24, and Moodo currently lack a useful verified audio intersection. |

Every proposed improvement must report native held-out quality, per-vibe song
ranking, source ablations, and propagation coverage separately. Keep source and
artist/audio identity groups intact across corpora. The existing 55-song direct
vibe diagnostic remains useful for regression checks, but repeated selection on
it cannot establish population accuracy. Larger external holdouts provide the
scalable evidence; regular-user vibe ratings are not part of this plan.

Train audio predictors from existing human song and excerpt annotations, then
use their learned activity and emotional signals to improve the existing six
vibes. Product users continue to use heart and skull for song preferences;
the model must scale without asking them to label vibes.

**Prioritize supplied audio.** Start with recordings already downloaded, then
datasets offering audio archives or direct recording downloads. Datasets that
need YouTube searches, recording verification, and sampling come later because
they will take substantially longer to produce training and evaluation results.
Downloaded annotations alone do not put a dataset ahead of one with usable audio.

## Preserve the existing model meanings and annotations

Keep `relax` (Relax), `concentrate` (Focus), `cruise` (Cruise), `energize`
(Energize), `uplift` (Lift my mood), and `release` (Let off steam), with their
[current definitions](../../src/yt_playlist/rec/home_vibe.py).

Retain the developer's human annotations and their original meanings. Keep
previous model predictions separately identified as predictions. External human
labels retain their native meanings and provenance; they are not automatically
ground truth for our six vibes. Human annotations can also disagree, so preserve
agreement information wherever the dataset provides it.

The experiments produce separate datasets, features, candidate models, and
reports. They do not import derived labels into the live database, rewrite
existing annotations, or automatically activate a model. The explicit product
checkpoint is a separate reviewed export of coefficients into the application.

## Acquisition status

Annotation files have been downloaded and examined for CAL500, NTWICM, DEAM,
Emotify, Soundtracks, Jamendo, VGMIDI, and CCMED/WCMED. The complete PMEmo audio
and annotation release is also local. Supplied audio is available for Soundtracks
Sets 1 and 2 (360 and 110 excerpts), CAL500 (500 of its 502 annotated recordings),
Emotify (400 excerpts), and PMEmo (767 choruses). Audio mirrors now also supply
DEAM's 1,802 recordings and the distinct Coimbra MIREX-like collection's 903
clips. Both joins have been checked against their original annotation identifiers.
Four official Jamendo audio shards yielded 216 recordings with complete explicit
human judgments; every archive and selected recording passed official SHA256 checks.
The separate 4Q release is also recovered: 900 clips, 225 in each quadrant, with
exact recording-ID joins and no folder/metadata label disagreements.
The author's VGMIDI audio is also local: 76 recordings passed the annotation
checks. A third-party Moodo XML copy has been recovered and examined (200 songs,
7,186 responses); its audio and exact recording identities are still missing.

Soundtracks Set 2, CAL500, Emotify, and PMEmo training completed on 28 October:
110, 496, 400, and 767 usable recordings respectively. Expanded Soundtracks
(470), the verified VGMIDI subset (76), Jamendo human annotations (216), Coimbra
(903), and 4Q (900) are also complete. The offline campaign is paused at the
user-requested product checkpoint; DEAM extraction is resumable. See
[running the experiments](EXTERNAL_EXPERIMENTS.md).

The [local acquisition manifest](local/datasets/manifest.json)
records the downloaded artifacts, native label formats, and outstanding access
problems. Those data files are ignored by Git. The source catalogue is
[datasets_emotion](https://github.com/juansgomez87/datasets_emotion); its captured
version contains 17 distinct datasets, with Moodo listed twice. Every distinct
dataset has a role below.

## Start with datasets offering audio

The order below favors time to a useful comparison. A working audio release can
advance immediately; an unavailable host must not hold up the other datasets.

| Dataset | Audio acquisition and priority | How we will use it |
| --- | --- | --- |
| [Soundtracks](https://osf.io/p6vkg/) | **First experiment:** use the 110 Set 2 excerpts already local. Download Set 1 directly afterward to expand coverage. | Train and evaluate predictions of valence, energy, tension, anger, fear, happiness, sadness, and tenderness. These provide contrasting emotional examples for calm, positive, and forceful music. Use the supplied labels for the actual excerpts. Set 1 and Set 2 overlap, so they cannot serve as independent train and test sets. |
| [CAL500](http://calab1.ucsd.edu/~datasets/cal500/) | **First comparisons complete:** 496 recordings usable; four supplied recordings are too short and two annotation rows lack audio. | Direct activity supervision for studying, reading, working, exercising, driving, sleeping, and social listening. Train activity predictors alongside mood and energy predictors. Preserve the supplied benchmark and separately test original listener fractions: its published activity matrices discard single-Yes choices rather than implementing the README's stated consensus rule. Activity judgments support our vibes; driving alone does not establish Cruise. |
| [Emotify](https://www2.projects.science.uu.nl/memotion/emotifydata/) | **Audio local:** join genre plus within-genre filename to the 400 global annotation identities. | Learn calmness, joyful activation, power, tension, tenderness, nostalgia, and the other native emotional categories. These offer richer evidence for Relax, Uplift, and Release. Keep the target of emotion felt by a listener distinguishable from descriptions of the music. |
| [PMEmo](https://github.com/HuiZhangDB/PMEmo) | **Complete audio and native annotations local:** join separate arousal and valence tables by music ID. | Add more recent mainstream popular music. Train overall activation and emotional positivity, plus changes over the labeled chorus. Preserve the excerpt boundaries rather than applying chorus judgments indiscriminately to the rest of a song. |
| [DEAM](https://cvml.unige.ch/databases/DEAM/) | **1,802 audio recordings local:** exact numeric IDs join the original annotation release to the [audio mirror](https://github.com/Akminar/Classifying-Emotionality-of-Music), pinned at `3db39c68bf69609b57cdc31dd46fa9f8fd24970f`. | Learn activation and emotional positivity at both section and overall excerpt level. Start with static listener means on the supplied audio; normalize the original 1–9 scale. Its changing annotations support a later section-aligned comparison. Recording-based grouping does not establish artist independence where identities are missing. |
| [Jamendo Mood and Theme](https://github.com/MTG/mtg-jamendo-dataset) | **Official audio reachable:** shard 00 is verified against official archive and track SHA256 files; shards 07, 23, and 53 expand the sample. Select exact human-annotation/audio intersections, not songs chosen by model predictions. | Train seven explicit human tasks: relaxed, happy, aggressive, party, sad, acoustic, and electronic, using positive-vote fractions. Keep disagreement. Missing uploader tags remain unknown. Original split-0 test annotations enter training, so use new artist-group validation and do not report split 0 as an untouched test. The source states noncommercial research terms; product use must be resolved before release training. |
| [Coimbra MIREX-like](https://mir.dei.uc.pt/) | **903 supplied clips local:** use the [audio mirror](https://github.com/ethanray123/AudioEmotionPredictionModel), pinned at `8bdd6492f520d76cc2fa74ff69885b2b2b9c5777`. Preserve its separate identity from original MIREX and 4Q. | Predict five native mood clusters derived from AllMusic tags. Exact numbered filenames and row-aligned cluster annotations determine targets; group artists. Audit the release's UTF-16 metadata and its one unquoted title semicolon before joining. |
| [4Q Emotion Dataset](http://mir.dei.uc.pt/downloads.html) | **900 supplied clips local:** use the [audio mirror](https://github.com/ritam413/Music-Mood-Classifier-), pinned at `3b29a3f47c669b7a0c992211d638641ca5079594`. Join exact AllMusic recording IDs to CP1252 metadata; group 664 artists. | Train the four combinations of high/low activation and positive/negative emotion. This separates energetic-positive from energetic-tense music and calm-positive from subdued-sad music. Report classification accuracy and class-balanced metrics alongside one-hot regression error. |

## Use slower acquisition routes afterward

These datasets remain valuable, but collecting or reconstructing their audio
must not delay the first results from supplied recordings.

| Dataset | Audio route | How we will use it |
| --- | --- | --- |
| [NTWICM](http://openaudio.eu/NTWICM-Mood-Annotation.arff) | **Supplied-audio intersection first:** exact artist/title joins find 58 candidate audio matches, before merging repeated song identities. YouTube acquisition remains a later route for the rest. | Add whole-song activation and positivity supervision across familiar mainstream music. Retain all four raters' judgments and disagreement. The local intersection is weak song-level supervision, not a reproduction of the original excerpt or benchmark split. |
| [CCMED/WCMED](https://www.metacreation.net/projects/ccmed_wcmed_soundscape/) | Recover original source recordings directly where possible. Western metadata supplies recording names and excerpt times. Use YouTube matching only for unresolved recordings; the retrieved archive contains no audio. | Train relative comparisons and rankings: which excerpt is more activated or more positive. This adds Chinese and Western classical coverage. Preserve the relative task instead of assuming its ratings share an absolute scale with other datasets. |
| [VGMIDI](https://github.com/lucasnfe/vgmidi) | **Native and consensus comparisons complete:** 76 of the 99 recordings passed unique title/franchise, duration, and complete-listener checks. | Run a separate instrumental auxiliary experiment using original whole-piece listener annotations. Group game franchises and retain this supplied-audio subset separately from the complete 200-piece/204-phrase release. Include it in the main mixture only if it improves evaluation on recorded music. |

## Recover the remaining releases

For these datasets, recover annotations and determine the audio route before
allocating training work. A release containing usable audio joins the earlier
queue. A release requiring YouTube matching joins the later queue. An inaccessible
release remains a documented acquisition gap rather than blocking the experiment.

| Dataset | How we will use it when the release is available |
| --- | --- |
| [Moods MIREX](https://www.music-ir.org/mirex/wiki/2020:Audio_Classification_(Train/Test)_Tasks) | Train its rousing, cheerful, brooding, quirky, and aggressive mood distinctions as a separate task. Rousing, cheerful, and aggressive judgments provide candidate evidence for Energize, Uplift, and Release. Keep the original release distinct from the separate Coimbra MIREX-like dataset. |
| [Yang-Dim](http://mac.citi.sinica.edu.tw/~yang/MER/taslp08/#Data) | Add activation/positivity examples across Western, Chinese, and Japanese popular music. Use these to broaden training and test dependence on English-language repertoire. |
| [MoodSwings](https://www.met-lab.org/emotion-recognition) | Train and evaluate emotion changes within a recording. Use time-aligned labels to improve sample-level predictions and song aggregation. The original release and the later MoodSwings Turk release must retain separate identities. |
| [DEAP](https://www.eecs.qmul.ac.uk/mmv/datasets/deap/) | Initially use music-video emotion ratings as an external evaluation task. Video can influence the ratings, so establish their usefulness for audio before adding them to training. Its YouTube identities can aid later matching; physiological and face recordings are not needed. |
| [AMG1608](https://amg1608.blogspot.com/) | **Original rating matrix recovered:** a [mirror](https://github.com/loichan-tw/AMG1608_release) pinned at `16ce26147f509e712c70cef99dc7dd0dfa912702` supplies 1,608 song identities and all 665 users' sparse ratings. Verify each split archive against its Git blob. At least 15 complete ratings per song; preserve six slight coordinate overshoots and validate song means. Roughly 400 exact artist/title identities match supplied local audio, so use their native VA judgments without new extraction. Record possible master/edit/excerpt mismatch. Supplied acoustic measurements are not substituted for our audio features. |
| [Moodo](https://lgm.fri.uni-lj.si/research/mood-and-music/) | A third-party XML copy now supplies 7,186 responses on 200 songs: 10 induced and 14 perceived emotion categories. Recover audio or verified SongID-to-recording mappings before training. Keep the two tasks separate, along with color associations and personal preferences. Preserve raw emotion placements rather than assuming they are interchangeable with other datasets' song-level activation/positivity ratings. The author endpoints still fail, so original-release authenticity is not checksum-verified. |
| CH818 | Add expert-labeled Chinese popular music for training and evaluation. Use it to check whether improvements extend beyond Western datasets. The catalogue marks it offline; a release still needs to be found. |

## Training and comparison sequence

1. **Establish the first external-data result with local Soundtracks audio.**
   Process its excerpts through the same audio extractor used by Radio. Compare
   the existing predictors with models trained on its native labels, using
   separate recordings for training and evaluation. This is a pipeline and
   domain-specific result, not proof of product-wide six-vibe quality.
2. **Add CAL500 and Emotify as their audio arrives.** CAL500 introduces direct
   activity evidence; Emotify adds richer emotional distinctions. Produce a new
   comparison after each addition rather than waiting for the whole catalogue.
3. **Expand with ready audio releases.** Add PMEmo, DEAM, an initial Jamendo
   subset, and 4Q according to actual availability. Keep dataset-specific label
   tasks while learning from common audio attributes and embeddings. Different
   scales and meanings must not be pooled as interchangeable labels.
4. **Combine the learned evidence for the existing vibes.** Document how native
   activity and emotion predictions support each vibe. Low activation alone
   does not establish Focus, and high activation alone does not establish
   cathartic Release. Preserve the developer's direct vibe judgments as the
   reference for those meanings, while external annotations supply training
   volume. Keep direct-vibe calibration and evaluation examples separate.
5. **Measure each addition.** Compare the current model, external-data training
   alone, and external-data training combined with the existing human
   annotations. Report native-task quality and six-vibe quality separately.
   Measure each dataset's contribution by removing it from the training mixture;
   retain additions that help without unacceptable regressions.
6. **Add matched recordings and recovered releases incrementally.** NTWICM and
   unresolved CCMED/WCMED recordings follow the supplied-audio experiments.
   Other recovered datasets enter according to their audio availability and
   measured contribution.

## Consensus combinations

Keep a separate expert for each dataset's native task, with its own calibration
to the unchanged six vibes. Include the embedding-only model as another member.
Compare every nonempty subset of the available experts. For every subset with
at least two members, compare equal probability averaging and learned convex
weights. This includes pairs, triples, larger mixtures, and mixtures that omit
the embedding model; the full ensemble is only one candidate.

With Soundtracks Set 2, CAL500, Emotify, PMEmo, and embeddings, this is 31 subsets,
including 26 consensus subsets. Adding VGMIDI makes 63 subsets, including 57
consensus subsets. Soundtracks-all is an alternate overlapping corpus, so it
does not count as an independent extra expert in the same mixture.

Use the same five artist-group outer folds for all candidates. Inside each
outer training fold, cross-fit each expert, fit candidate pooling weights, and
choose the subset and pooling policy using acceptable-label log loss. Evaluate
that selected candidate only on the untouched outer fold. Report both a search
restricted to multi-expert combinations and a search that also allows a single
expert to win. The inner scores fit and select weights on the same cross-fitted
predictions; they are selection objectives, not independent accuracy estimates.

Retain fixed-combination outer scores, chosen members, weights, error
correlations, and per-song predictions. The best fixed combination found after
looking at those scores is exploratory; it is not a separately validated winner.
The existing 55-song developer snapshot is small and previously examined, so
neither the exhaustive search nor consensus establishes product-wide quality.
Do not turn model agreement into human labels or activate a research model.

The next campaign compares linear ridge, radial-basis kernel ridge, and nearest
neighbours under the same grouped outer folds. Inner folds choose their parameters
and can choose the learner family itself. Test these on each native corpus and on
the fixed six-vibe diagnostic, then compare Soundtracks-all as a replacement for
Set 2. Add Jamendo, Coimbra, 4Q, and DEAM separately before evaluating their combined
expert subsets. The 47-song Jamendo pilot and its expanded sample likewise replace
one another; overlapping versions are not independent consensus votes.

Then cross three native-head choices (ridge, radial-basis kernel ridge, and
inner-selected learner) with linear, radial-basis, and softmax vibe calibration.
For each pairing, repeat every consensus subset; retain nested selected scores
separately from the best fixed combination found by inspecting the results.
With nine external experts and the embedding expert, this means 1,023 nonempty
subsets per pairing.

Also evaluate the product's actual use: ranking songs for one chosen vibe. Keep
the six existing positive-label sets and fit six independent binary suitability
scores, rather than forcing every song's scores to sum to one. Compare each
expert and every expert subset using per-vibe average precision and AUC. Learn
separate convex mixture weights for each vibe inside the same nested artist
folds; select parameters and subsets by class-balanced binary log loss. Retain
both the multi-expert-only selection and the selection that permits one expert.
These balanced scores are ranking scores, not calibrated population probabilities.
The existing annotations list acceptable alternatives; an unselected alternative
is not an explicit human rejection. Report this limitation and keep the original
acceptable-top-choice diagnostic as a secondary measure. No labels are rewritten.

Follow-up trials reuse these extracted features: static versus time-aligned
DEAM/PMEmo labels, a duplicate-free repeat of augmentation consistency, and
PCA/whitening learned only from other corpora's unlabeled audio. Select all task
parameters inside grouped folds, preserve each native target, and retain negative
results. None of these trials changes the six product vibe labels.

Finally test a small shared nonlinear audio representation, with a separate
output task for every corpus. Balance the task losses rather than merging their
VA scales or meanings. Compare individual ridge heads, supervised shared training,
and shared training with an optional same-excerpt consistency penalty. Global
artist/audio connected components define the folds, and every known gold identity
is removed from external training. Withhold native labels for the 127 unique
unlabeled training recordings and every known matching excerpt in other corpora.
Only unlabeled pairs whose audio belongs to the current training partition may
contribute; remove the fixed 32-pair robustness holdout and its known identity
matches entirely. Withheld labels never enter fitting, parameter selection, or
the primary native comparison. Choose representation parameters on native validation,
then fit and evaluate separate six-vibe calibrators using the existing snapshot.

Compare one center excerpt with equal means over early, center, and late excerpts
for Emotify. Its listener labels concern the longer supplied recording, and the
gold embeddings already average available short samples. Keep every recording,
label and validation group fixed. Stay slightly inside the recording boundaries
to avoid compressed-audio padding; allow overlap on the few shorter recordings
and record every window. Reuse the center cache and extract the two added views
sequentially. The averaged corpus replaces Emotify in any mixture.

Add a separate EMMA preview experiment after the expanded campaign. The official
site's Expert CSV supplies 817 aggregated song annotations; 333 GEMS-45 rows join
unambiguously by artist/title to the original public song codes. Download those
codes through the site's published audio-player path. The current previews are
about 15 seconds, shorter than the original rated excerpts, and their precise
alignment is unverified. Treat this as weak supervision on current previews,
not a reproduction of the original EMMA benchmark. Train the nine native GEMS
dimensions, excluding liking and familiarity, and compare with Emotify and
PMEmo individually and in consensus. Include it in the larger source search
only after its native run completes. Preserve the public CSV hash, rejected
joins, audio hashes, rater counts, and excerpt mismatch in the artifacts.

Also run a fixed semantic mapping with no developer-label fitting. Map native
GEMS or valence/arousal predictions to the existing six purposes using explicit
equal-weight rules, and test equal source pooling. A separate fixed variant
adds CAL500's direct concentration and exercise activities for those purposes.
Use external training predictions for reference percentiles; compute every gold
prediction before scoring it. Preserve the rules before executing the trial.
These approximate semantic assumptions are a baseline for reducing reliance on
developer calibration, not new definitions or independently validated mappings.

The newer [MERGE Audio Complete](https://zenodo.org/records/13939205) release
offers 3,554 supplied 30-second clips and extends 4Q. Download its official
archive, verify the published checksum, and audit recording and artist overlap.
Compare its manually validated native quadrants with the existing 4Q model,
reusing exactly matching extracted audio. Treat the tag-derived continuous VA
coordinates as a separate weak task, since they were not listener-rated.
Replace 4Q with MERGE in any expert mixture rather than counting the overlapping
datasets as independent votes. Preserve the supplied benchmark splits alongside
our artist-group diagnostics.

Two additional annotation collections currently lack a useful audio intersection.
TROMPA-MER has only one strict artist/title match in the acquired audio; its
publisher preview endpoint timed out. FME-24 explicitly distributes metadata and
measurements without copyrighted audio, and has only two strict local matches.
Keep both acquisition gaps documented. EMOPIA's public release supplies MIDI and
YouTube identities rather than the original audio; it remains on the slower
recording-acquisition route.

Two targeted checks follow the expanded models. First, test listener-count
weighting on Emotify, with unweighted training included in native parameter
selection; more ratings alone do not establish better judgments. Second, train
native VA predictors on one of PMEmo, DEAM, matched AMG1608 and matched NTWICM, then
evaluate directly on each other corpus. Exclude known target identities before
source training and select every parameter on source data only. These tests
measure transfer across annotation populations without merging their targets or
using the developer's vibe labels for selection.

Run `python -m experiments.audio_lab_tools.external_campaign` in the project
environment for the sequential campaign. It checkpoints each stage, records
failures, preserves separate trial artifacts, and refreshes the local results
table after each completed stage. Continue follow-up trials when evidence supports
them; a temporarily empty original queue is not the stopping condition.

Keep duplicate recordings, overlapping excerpts, and related versions together
when splitting data, including overlaps across datasets. Use artist separation
where practical. Audit overlap with the current pretrained embeddings and mood
heads: a new head's held-out score does not establish independence from an
existing model's training data. Jamendo's derived annotations reuse its original
split-0 test pool, so training on those annotations invalidates that pool as an
independent test of the candidate model.

## Learning from unrated Radio playback

Once the external labels establish a stronger supervised foundation, test an
additional learning objective on unrated Radio audio: consistency across
augmentations of the same excerpt and better representation of repeated
observations. Do not force distant sections of a changing song to have identical
moods. Recheck the candidate against the fixed external and direct-vibe holdouts.
This gives unattended playback a concrete experimental role without turning the
model's predictions into human labels or requiring regular users to rate vibes.
