# External datasets for the vibe model

Related issues: #530 (current work), #507 (earlier experiments), #406, #405 and
#136. Acquisition catalogue updated on 29 October 2026; additional dataset and
paper research recorded on 1 November 2026 at the end of this document.

## Product checkpoint and next experiments

Research resumed under #530 after the earlier product checkpoint. Keep all
research changes and artifacts in `experiments/`; retain downloaded audio,
extracted features, and unfinished trials. Emotify's linear native-emotion
expert with linear six-vibe calibration remains the product comparator. See
[current experiment evidence](EXTERNAL_EXPERIMENTS.md) and
[model evidence and integration](MODEL_CHECKPOINT.md).

Provider audio metadata supplies initial vibe estimates. Normal playback
sampling then adds actual measurements, which feed the learned model and seed
bounded artist/genre predictions for unmeasured recordings. Only the developer
vibe-rating UI is opt-in; ordinary collection must not depend on that switch.
Predicted recordings never become independent measurement votes. Heart/skull
continues to mean song preference, and regular users never supply vibe labels.

The source comparisons and one-time reserved-label evaluation are complete.
No replacement is established. Decoder sensitivity and the bounded fresh/cold
preparation checks are complete. The cold queue remains unfinished at its
recorded time bound. A separate fresh consumer replay now demonstrates fallback
Mix Tape/Radio output with title/artist data alone, 77 content vectors after
copied genre/year metadata, and 462 direct vibe measurements after three captures.
This bounded cohort does not establish signed-in account latency or musical fit.
The five fixed library candidates have all been attempted: four complete the
extended diagnostic, while activity semantics reaches its fixed warm-retune
deadline. The original publication failure and that retune failure remain
explicit; no replacement is established. A separate semantic restriction
continuation reproduced all ordered playlists and found zero violations without
retrying warm retunes. Exact results are in
[the experiment evidence](EXTERNAL_EXPERIMENTS.md).

| Priority | Data | Next use |
| --- | --- | --- |
| 1 | Emotify, 400 supplied excerpts | Artist-grouped, early/center/late, listener-count and reserved comparisons are complete. Preserve nine native emotions and unchanged six-vibe meanings; local additions do not establish a replacement. |
| 2 | DEAM, 1,801 usable recordings; PMEmo, 767 choruses | Static/aligned native tasks and cross-corpus transfer are complete. New ManyMusic/Memo transfer adds separate human evidence; no target-corpus selection. One DEAM measurement decode has a one-sample boundary exclusion, audited below. |
| 3 | CAL500, 496 usable recordings | Original activity-vote ranking, native learners, expanded consensus and fixed-semantic reserved comparisons are complete. The fixed activity mapping worsens reserved NLL; direct evidence for Focus remains weak. |
| 4 | EMMA, 333 downloaded publisher previews | Native GEMS, standalone, and consensus comparisons are complete. Preview alignment remains unverified, so this stays weak supervision. |
| 5 | MERGE Audio Complete, 3,553 usable clips | Native quadrant classification, separate tag-derived coordinates, and 4Q expansion comparisons are complete. One extraction failure remains recorded. MERGE and overlapping 4Q enter alternative mixtures. |
| 6 | AMG1608 and NTWICM annotations | Native comparisons use 404 and 55 strict supplied-audio intersections. Keep song/excerpt mismatch explicit; further track-list recovery remains lower priority. |
| 7 | Soundtracks, 4Q, Coimbra, VGMIDI, Jamendo | Native tasks, learner comparisons, source ablations, every nonempty selected-pool consensus subset and historical paired uncertainty are complete. No post hoc subset winner is selected. Source versions remain alternatives; Jamendo terms remain explicit. |
| 8 | Unrated audio pairs and all ready native corpora | Duplicate-free consistency, PCA/whitening, local prediction ablations, shared-task comparisons and frozen reserved evaluation are complete. No reliable local-data gain is established. Model agreement and playback duration remain separate from human truth. |
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
(903), and 4Q (900) are also complete. The #530 extraction campaign now has 1,801
usable DEAM recordings and 3,553 MERGE clips; one audio interval in each release
failed extraction and remains recorded. It also completed artist-grouped
Emotify, original CAL500 activity votes, Emotify+ and MERP.
The later exclusion audit identifies a 4.778667-second MERGE file
(`MT0002054074`). DEAM `2048` is 151.91 seconds long; timestamp-based trimming
returns 440,999 rather than 441,000 stereo measurement frames, while its
16 kHz model decode is complete. This is a one-sample boundary artifact, not
short source audio. Preserve the completed 1,801-row comparisons and frozen
reserved evaluation; record any recovered feature in a separate later audit.
Emotify+ retains 265 identity-consistent tracks from its 399 audio joins;
MERP supplies 54 verified WAVs, with four DEAM anchors excluded from its 50-song
weak static pilot. See
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

## New audio-backed leads audited on 29 October

[ManyMusic-Stim v2](https://zenodo.org/records/23647077) supplies audio and
[human validation ratings](https://manymusic.net/stim/plots_bhv.html). A bounded
ZIP-range inspection recovered metadata using 1.47 MB instead of downloading the
30.4 GB archive. Exactly 283 supplied recordings intersect the ratings: 188
natural Jamendo tracks and 95 generated Suno tracks. All 283 members are now
downloaded (2.19 GB compressed) with ZIP CRC checks and recorded SHA256 hashes;
the annotation audit has rechecked the acquired audio. The full archive's MD5
is unverified because only selected members were acquired. All rated recordings
now have complete features for the fixed recording-balanced transfer pilot.

The local audit finds 1,695 distinct rated windows and signed VA coordinates in
[-1, 1]. Average repeated responses within each listener/window before averaging
listeners: 112 repeat responses include 22 conflicting listener/window sets.
Keep recorded thirty-second excerpt positions, all windows of a recording and
Jamendo artist IDs together. Group generated tracks by prompt, report them
separately, and exclude prompt words and supplied model predictions from targets.
Apple/Spotify recordings lack supplied audio here. Jamendo overlaps and
MusiCNN-based source curation limit independence. Track-specific Jamendo terms
and the release's noncommercial terms remain explicit. The verified publisher
attribution file now joins all 188 natural recordings to their artist credits,
source URLs and individual licence URLs. The [publisher's source page](https://manymusic.net/stim/)
specifies CC BY-NC-SA 4.0 for Suno audio, metadata and human ratings; natural
recordings retain their individual Jamendo licences.

[Memo2496](https://figshare.com/articles/dataset/Memo2496/25827034) offers 2,496
instrumental MP3s and expert VA trajectories. Metadata and average-label CSVs
passed the publisher's MD5 checks; archive directory inspection used 324 KB.
A deterministic filename-hash sample selected 64 recordings before examining
targets, avoiding the initial 12.3 GB full download. All 64 MP3s are now local
(316,405,013 bytes), with member CRC checks, SHA256 records and verified joins
to both expert trajectories. Feature extraction and all fixed transfer
comparisons are complete. One pilot recording lacks complete annotations for the fixed
15–75-second interval and is excluded, leaving 63 eligible recordings. Preserve
blank late timestamps as missing observations. The [source protocol](https://arxiv.org/html/2512.13998v4)
uses signed coordinates and a 15–75-second analysis segment. Match actual covered
audio and labels without presenting padding as music. Artist metadata was
removed, so this is initially a cross-corpus transfer/pipeline pilot with unknown
artist independence. Emotion anchor words and radial distance are not our six
vibes or the product's shared intensity. The record lists CC BY 4.0; retain
source/audio provenance separately.

Both sources advance ahead of new track-list recovery. Their native human
annotations provide external evidence; they do not automatically label our six
vibes. Acquisition and annotation audits live under ignored
`local/datasets/discovery/`, with tooling in `dataset_range.py` and
`dataset_audit.py`.

The first transfer plan is now frozen under `checkpoint-1/new-corpora/plans/`.
It selects one exact thirty-second ManyMusic window per recording by a target-
independent identity hash and covers each eligible Memo interval with six
ten-second chunks: 1,227 chunks across 346 recordings. Both mean-embedding and
mean-prediction aggregation are fixed before evaluation. Source-only grouped
regularization selection uses PMEmo and DEAM static/aligned targets; neither new
dataset nor local labels fit or select these transfer models.

The completed overlap audit flags ten natural recordings sharing known corpus
identities or artists with previously used data. The primary slice excludes those known overlaps;
all-audio results remain descriptive. Exact normalized artist credits add a
cross-corpus check, while generic “Various Artists” credits remain unresolved.
The final transfer rebuilt this audit after the full checkpoint. Unknown aliases, re-encodings, backbone
pretraining and absent Memo artist identities prevent claiming full independence.
All 346 planned intervals now have complete extracted audio coverage. A strict
floating-point equality had falsely rejected 21 decimal-timestamp windows;
the corrected run preserves those original results and verifies cache lineage.
Natural
and generated ManyMusic audio are separate strata, with artist/prompt-group
uncertainty intervals. This bounded pilot precedes training on the new corpora
or expanding Memo acquisition.

Run this work through `./experiments/run.py`. `pilot --after-checkpoint` queues
focused checks, the refreshed overlap audit, aligned extraction and transfer
sequentially after the current heavy campaign; it stops on failures. The new
interval/provenance, consistency, reserved-artifact and consumer-adapter guards
have passed in the 46-check focused run. The corrected transfer and all 26
source-comparison stages, reserved evaluation and four-recording actual browser
capture are complete. Decoder sensitivity and fresh/cold/paired library checks
continue sequentially through the same approved entrypoint.

Two further supplied-audio leads now have bounded, reproducible metadata audits.
Run `./experiments/run.py leads acquire song-describer` or `leads acquire emid`;
these commands acquire metadata only and preserve checksummed publisher inputs.

[Song Describer](https://github.com/mulab-mir/song-describer-dataset) offers
human captions and a direct audio archive. Its released, publisher-MD5-verified
CSV contains 1,106 captions for 706 recordings; the validated flags retain 746
captions on **547** recordings, differing from the data card's 546. Exact
Jamendo track/artist checks against our human Jamendo and ManyMusic collections
leave 246 validated recordings without known overlap. Preserve this as an
evaluation-only semantic lead, consistent with the publisher's single-split
intent. Missing words in a caption are unknown, not negative vibe labels.
Audio has not yet been acquired; retain per-recording terms from the supplied
licence file and unresolved upstream training overlap.

[EMID](https://huggingface.co/datasets/ecnu-aigc/EMID) supplies embedded audio
in eight Parquet shards. The pinned publisher revision
`c414773bbe9531cd13c039c40c1a554d9317ef47` has 10,738 unique filenames but only
**1,836 original clips**. All 8,902 augmented rows repeat an original human
annotation bundle; they are not additional independent judgments. A simple
filename-parent join resolves only 5,925 augmented rows, so it is insufficient
for grouped validation. Begin with original clips, preserving human feeling
fractions and eleven named experience dimensions **only if a separate short-clip
protocol is established**. The [paper](https://arxiv.org/html/2308.07622v2)
states original clips span 3.7–9.4 seconds, mostly around five seconds: none
provides our required ten seconds of actual audio. Do not pad them or borrow
labels for longer retrieved recordings. Its expanded clips inherit labels
through audio-similarity selection rather than fresh listening judgments.
This limits immediate value for the current model despite direct audio access.
One original has a missing
dimension, repeated in four children; retain missingness instead of filling it
as a measured value. Verify dimension ordering, original audio provenance,
excerpt alignment and recording/artist groups before a bounded audio pilot.
Generated image captions and image-emotion labels are excluded from music
targets. No audio has been acquired, and this audit starts no new training.

Other leads receive bounded access checks before any new acquisition:

| Lead | Current evidence and disposition |
| --- | --- |
| [MusAV](https://mtg.github.io/musav-dataset/) | Human pairwise activation/positivity judgments for 2,092 previews are a useful independent ranking task. The [archived audio](https://zenodo.org/records/7448344) requires access approval. Keep this ahead of recording recovery once access exists; no access request has been sent. |
| [GlobalMood](https://github.com/harin-git/GlobalMood) | `leads acquire globalmood` pins revision `975ec1ece254adf293e25eb304591dd5688b13d3` and verifies Git blobs. The actual CSV contains 1,180 distinct YouTube IDs, twenty songs for each of 59 curation countries, rather than the README's 1,182. Its 117,996 mean-rating rows cover five participant countries and twenty native tags per country; preserve those populations separately. Fifty-three standard deviations are missing, with finite means in 1–5 and 1–18 ratings per row. An exact complete artist/title scan across ten existing audio-feature manifests finds just one song, Wham!'s “Wake Me Up Before You Go-Go”, with unverified recording/excerpt identity. This does not support a useful benchmark or substantial audio recovery now. No audio was downloaded and no model was fitted. |
| [MuChin](https://github.com/CarlWangChina/MuChin-V2-6066) | Chinese professional/amateur human descriptions and advertised supplied audio could broaden repertoire coverage. The 6,066-song Hugging Face release is gated and packages audio in a 26 GB archive. Keep behind usable acquired audio. Its source terms prohibit commercial audio training without holder authorization. The [v1 metadata documentation](https://github.com/CarlWangChina/MuChin/tree/main/dataset) identifies 724 overlaps between its 1,000-song version and v2; versions cannot be independent evidence. No audio or annotation corpus has been acquired. |
| [Moods and activities](https://reshare.ukdataservice.ac.uk/852024/) | Secondary Focus lead, with no supplied audio established. Separate judgments made while hearing recordings from word/activity association tasks before treating it as supervision. Only bounded existing-audio intersections merit immediate work. |
| [PHENICX-emotion](https://www.upf.edu/web/mtg/phenicx-emotion) | Publisher supplies fifteen mono WAV excerpts from one Beethoven symphony and acoustic descriptors, for academic research. The download listing alone does not establish usable human label files. Its narrow piece/performer coverage makes it a later diagnostic rather than a scalable independent corpus. |

The 29 October concentration/activity search checked the following primary
releases. None yet establishes an additional open, supplied-audio benchmark for
Focus. Keep the acquired CAL500 listener votes as the current direct activity
evidence and preserve their disagreement. These access observations are not
claims that the underlying experiments lack value.

| Lead | Evidence and disposition |
| --- | --- |
| [Music and Cognitive Performance](https://zenodo.org/records/8421227) | Describes task outcomes for 41 participants and Spotify identifiers/features for 17 stimuli. The listed files are restricted; a [later record](https://zenodo.org/records/14946483) is also restricted and reports a newer version. Latest access remains unresolved. The [experiment-code tree](https://github.com/amresearchlab/yiting-study-2024/tree/c7de1fc87c40f45f0a559e488b272971fa2f36ed) contains 187 files and no conventional music-audio files. Task-level music/control outcomes cannot be treated as independent per-song Focus labels. No files were acquired and no access request was sent. |
| [Help Me study!](https://zenodo.org/records/10085104) | Lists five spreadsheets, including experience-sampling context, track identifiers and Spotify features; no supplied audio is listed. Studying while hearing a track is observational usage, not evidence that the track improves concentration. Defer track recovery. |
| [Context-Aware Mobile Music Recommendation for Daily Activities](https://smcnus.comp.nus.edu.sg/archive/pdf/2012-2013/2012_Context.pdf) | The paper reports 1,200 songs judged by all ten listeners, with studying/working Fleiss kappa 0.09/0.03. Audio was recovered from YouTube after Grooveshark playlist discovery. No open packaged audio/annotation release was established in this search. Preserve listener fractions if recovered; do not assume agreement or objective task-performance labels. |
| [Music in Use: activity corpus](https://repository.tudelft.nl/file/File_4042adbe-223b-4deb-a363-12c02a429775) | Describes 2,589 YouTube items; activity categories derive from titles. The release is described as video IDs, rather than supplied audio. Keep behind human judgments with supplied recordings. |
| [Music in Use: derail moments](https://osf.io/n3zmp/) | The same thesis describes distraction judgments during transcription and a release of YouTube IDs plus moment annotations. Potentially useful for temporal distraction, not interchangeable with general Focus suitability. Direct OSF access was unresolved; supplied audio was not established. No recovery started. |

The Release follow-up distinguishes expressed anger, felt emotion and the effect
of listening on an already angry person. These are different targets. None of
the following establishes per-recording cathartic suitability for our library.

| Lead | Evidence and disposition |
| --- | --- |
| [Individual musicians playing emotional music](https://zenodo.org/records/10396364) | `leads acquire playing-musicians` recovered the questionnaire and spreadsheet with 185,850 bytes of checked archive ranges. The 59 unique label rows join 56 originals across eleven performers; `ms09_1`, `ms09_2` and `ms09_3` lack supplied audio. All four VA fields contain valid integer ratings in 1–5. The matched intended conditions are Angry 14, Happy 13, Relaxed 16 and Sad 13. The 168 split WAVs are dependent observations. Preserve intended, communicated and performer-felt targets separately and group performers. Publisher metadata lists CC BY 4.0; questionnaire members passed CRC/SHA256 checks, while the 1.3 GB archive checksum and audio durations/content remain unverified. No audio acquired or models fitted. |
| [MUSMET VR](https://zenodo.org/records/18642655) | One musician performs eight pieces under four emotion conditions; the 4.2 GB release lists video and EEG. No independent listener ratings or usable standalone audio were established. Its small performer count and acquisition cost put it behind the eleven-musician lead. |
| [Extreme Metal Music and Anger Processing](https://www.frontiersin.org/journals/human-neuroscience/articles/10.3389/fnhum.2015.00272/full) | A controlled study of 39 existing extreme-music listeners compares self-selected music with silence after anger induction. Anger-related ratings decline in both conditions, while active/inspired responses differ. Its table lists 46 song identities; no supplied-audio or participant-to-song outcome release was established. This supports preserving listener/context dependence, rather than labeling angry-sounding songs as effective Release tracks. Track-list recovery is deferred. |

The musician audit is reproducible with `./experiments/run.py leads inspect
playing-musicians`. Its `local/datasets/discovery/playing-musicians/` directory
retains exact publisher metadata, range responses, extracted questionnaire
members, native rows, joins and exclusions. Only the documented Sheet1 fields
are labels: the workbook also contains formatted empty rows and a separate
six-name scratch sheet. They do not add observations. Keep this small native
diagnostic behind the outstanding fixed library replay; it does not fill the
missing independent Release-suitability judgments.

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

Use the approved `./experiments/run.py` entrypoint for sequential research work.
`checkpoint`, `complete` and `pilot` preserve their pinned stage receipts; `evidence`
collects saved results without fitting or selecting again. Library diagnostics
accept the user-authorized independent execution flag. Preserve completed frozen
accuracy results and negative trials. New fitting requires a new protocol and
evaluation cohort; a completed queue is not a reason to repeat selection on the
reserved labels.

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

## Additional research, 1 November 2026

There are useful directions beyond adding more static emotion corpora. The
strongest immediate hypotheses concern information missing from the current
audio representation: intermediate MERT layers, explicit harmonic features,
and human judgments of perceptual musical properties. A verified publisher
listing also gives the existing WCMED annotations a substantially better audio
route. These are research proposals, not evidence for replacing a product model.
All proposed implementation and acquisition can stay inside `experiments/`.

### What would actually be new

Emotify+, MERGE, and a weak static MERP pilot already exist here. So do shared
nonlinear representations with separate native corpus targets in
`audio_lab_tools/external_multitask.py`, static versus aligned DEAM/PMEmo
targets, consistency training, and multiwindow Emotify/ManyMusic comparisons.
Repeating those ideas under a new name is not the next experiment. The recent
MERT comparison (#708), however, extracted only a mean of the final transformer
layer. It did not test intermediate layers or harmonic descriptors. See
[the encoder protocol](ENCODER_COMPARISON_PLAN.md) and
[its retained results](ENCODER_COMPARISON_RESULTS.md).

| Order | Concrete experiment | New evidence it could provide |
| --- | --- | --- |
| 1 | Fixed MERT layer-5/layer-6 representation versus the existing final-layer representation | Tests whether the recent negative/mixed encoder result depends on layer choice, without another dataset acquisition. |
| 2 | Existing embeddings plus a fixed 72-dimensional chroma summary | Tests whether harmonic information improves native emotion prediction and transfer beyond the existing embeddings. |
| 3 | Acquire and audit the 5,000-excerpt mid-level perceptual release; start with verified local audio intersections | Adds human musical-property supervision rather than another remapping of the same emotion categories. Downloads remain unresolved here. |
| 4 | Recover the exact WCMED source recordings from the maintained Saarland publisher page | Adds a separately reported classical ranking/transfer test using already acquired annotations. |
| Later | Audit MERP timeline alignment, then compare time-aligned and weak-static training | Uses information discarded by the current 50-song static pilot. Exact timing must be established before fitting. |

### Intermediate MERT layers and harmonic information

Kang and Herremans' [unified emotion-recognition paper, version 3](https://arxiv.org/html/2502.03979v3)
uses the same MERT-v1-95M family but concatenates layers 5 and 6. It also reports
gains from chord/key information. This supports two distinct hypotheses; it does
not establish that either transfers to our ten-second inputs or six purposes.
The paper uses longer segments and random VA splits, so its scores are not
directly comparable with our artist-grouped results. EmoMusic is a DEAM subset;
their names must never imply independent training/test corpora here.

For the layer experiment, retain exactly #708's 288-recording, 576-chunk
comparison cohort, PCM intervals, model revision, grouping, native ridge heads,
and mapping procedure. Predeclare final-layer mean versus concatenated means of
transformer layers 5 and 6. Record the precise hidden-state indexing, including
whether index zero represents the convolutional input. Keep preprocessing
variants paired and select ridge penalties only within training groups. The
stored final-layer vectors cannot reconstruct middle layers: extraction must
run again on the same PCM. One encoder forward pass can expose all required
hidden states. Recompute resource bounds rather than assuming their memory
cost equals the last-layer worker's. This cohort is already explored, so its
results would be development evidence, not fresh confirmation.

Ching and Widmer's [distribution-gap study](https://arxiv.org/html/2510.04688v1)
finds poor cross-corpus transfer despite apparently similar VA labels, and tests
chroma alongside pretrained embeddings. Its 72-dimensional representation
summarizes twelve chroma bins with means and standard deviations of the signal
and its first two differences. This is a smaller, more isolated next test than
copying a complete chord Transformer or training another large encoder. The
paper's reported DEAM transfer includes a source dataset overlapping DEAM, so
its headline transfer score is not independent confirmation for our use.

Extract chroma from the exact rated intervals used by our baseline. Compare
embedding only, chroma only, and concatenation with the same native targets,
folds and regularized head. Fit all feature scaling within training partitions.
Keep native corpus scales separate and exclude target identities before any
source-only transfer fitting. Compare popular, soundtrack, instrumental and
classical domains separately. If the chroma addition helps, chord/key features
become a justified second-stage ablation. A lower emotion error alone does not
establish better Focus or cathartic Release recommendations.

### New auxiliary data: human mid-level musical properties

Aljanaki and Soleymani's [mid-level perceptual dataset paper](https://arxiv.org/html/1806.04903v1)
describes 5,000 excerpts with human ratings for melodiousness, articulation,
rhythmic complexity, rhythmic stability, dissonance, tonal stability and modality.
Most new material comes from Jamendo/Magnatune, with approximately fifteen-second
excerpts. Existing emotion corpora, including Soundtracks and the 903-song
Coimbra collection, are reused. These are not 5,000 new independent emotion
examples. Agreement is weaker for rhythmic complexity and tonal stability.

The [OSF project](https://osf.io/5aupt/)'s public API was reachable. It lists:

| File | Published bytes | Published SHA-256 |
| --- | ---: | --- |
| `metadata_annotations.zip` | 318,090 | `f0a36e901ed6d9e4f21d61d49cd2631642140e3329bb7e41c7524e09688fe1ac` |
| `audio.zip` | 1,182,493,865 | `69c33f323ab8cdcbf915be643088a5ec88aee167b2b4543c9fc4e1a5a7d78c0e` |

These are publisher hashes, not locally verified archive hashes. OSF download
requests returned HTTP 403 or timed out; neither archive was acquired. The
project API exposes no node license. Verify the archive README, per-recording
terms, actual rating scales and which reused clips are supplied before fitting.
The papers differ in their description of rating endpoints, so do not infer
the parser's scale from prose alone. No numerical local intersection has yet
been established for this release.

Chowdhury et al.'s [mid-level emotion model](https://arxiv.org/html/1907.03572v1)
provides a concrete use: predict the musical properties as an intermediate
representation, then predict native emotions from them. Compare direct audio
prediction, predicted-property augmentation, and an optional property bottleneck.
Use out-of-fold property predictions for training downstream heads; exclude
held-out artists/recordings from every auxiliary fitting stage as well. Published
pretrained property models can already include Soundtracks, so their provenance
cannot be assumed independent. Human properties supplied at test time would be
an oracle diagnostic, not deployable performance. Rhythmic stability and
complexity are candidate explanatory signals, not concentration labels.

### WCMED: a verified source-audio route

The acquired WCMED release contains **400 excerpts from 61 source recordings**,
with exact start/stop times. This differs from the piano subset/counts described
in the later distribution-gap paper. Its README identifies the Saarland Western
Music collection; the separate Disklavier MIDI-piano archive is not a substitute.

An exact filename join against the maintained
[Saarland Western Music publisher listing](https://www.audiolabs-erlangen.de/resources/MIR/SMD/western-music)
finds **387 excerpt rows across 58 recordings with download links**. Of those,
**288 excerpts supply at least ten seconds** according to the released metadata.
Three recordings have no linked download: Debussy `L140-01_107_20070109`,
`L140-02_107_20070109`, and `L140-03_111_20080519`. A HEAD request to one matched
MP3 returned HTTP 200 with an audio content type and 9,182,144-byte length.
That establishes a working example endpoint, not successful acquisition or
alignment of the entire set. No audio was downloaded in this audit. The source
page states CC BY-NC-SA 3.0 terms.

Recover only listed original recordings; verify decoding, durations, excerpt
alignment and the original short attack/decay treatment. Exclude excerpts under
ten seconds from this protocol instead of padding them or borrowing extra
music. Keep same-recording excerpts together, connect related compositions and
anonymous performer identifiers, and report the resulting independent group
count before defining folds. Multiple excerpts are not independent recordings.

The release supplies global arousal/valence rankings and linear conversions of
rank to a continuous value. Those converted values are not separate absolute
listener ratings. Begin with a frozen source-only transfer test scored by native
rank agreement, with uncertainty clustered by recording and related groups.
Do not call the derived rank pairs raw pairwise votes. If the source then becomes
training data, reserve a separate split first and retain its source identity.

### SiTunes: relevant outcomes, insufficient current audio intersection

The [SiTunes author release](https://github.com/JiayuLi-997/SiTunes_dataset)
contains listening observations with before/after valence and arousal. This is
closer to mood change than perceived-emotion tags, but remains observational.
The downloaded and Git-blob-verified metadata, pinned at
`aea53feca9ba092406b5b95a3976dba0b44c568f`, yields:

- 2,006 interactions from 30 participants on 307 distinct items.
- 1,406 before/after interactions in stages 2 and 3; stage 1 lacks before ratings.
- 936 item-metadata rows, with nine listened item IDs absent from that table,
  affecting 153 interactions across all stages.
- Twelve distinct artist/title candidates in our acquired audio-feature pool,
  covering 69 interactions, of which only **39** have before/after ratings.

These are normalized exact artist/title candidates, not verified recording or
excerpt matches. Repeated appearances in feature manifests do not add items.
No music audio or explicit repository license file was found in the pinned tree.
A generalizable audio mood-change model is not supported by this intersection.
The useful next step is a metadata feasibility study: separate initial mood,
participant and context effects before considering audio recovery. Compare
against initial-mood and participant baselines, and hold out participants and
tracks where feasible. Spotify's supplied valence is an existing model feature,
not a human outcome. VA change provides neither anger-relief truth nor cognitive
task performance. This route requires no new ratings from TuneConsole users.

### Lower-priority leads and research receipts

| Lead | Finding and disposition |
| --- | --- |
| [CP-WTC](https://github.com/CPJKU/The-CP-WTC-Dataset) | 288 first-eight-bar excerpts of 48 Bach pieces performed by six pianists, with 7,652 individual human annotations and released features. Commercial recording audio is explicitly excluded. Useful for a feature-level performance diagnostic; not immediately usable with our waveform extractor. Keep pieces and performers explicit and never count snippets as new compositions. |
| [MuMeDB v2](https://data.mendeley.com/datasets/9zdww6wnyx/2) | Publisher advertises 1,412 multimodal records and CC BY 4.0. File-list requests failed; fresh human-label provenance, recording overlap and usable supplied audio remain unverified. Do not book 1,412 new training tracks from the description. |
| [MusicSem](https://huggingface.co/datasets/AMSRNA/MusicSem), [paper](https://arxiv.org/html/2602.17769v1) | Reddit-derived language and track identifiers, with LLM processing; no packaged audio, and the paper's human-validated test set is private. Potential weak situational retrieval supervision, with thread/recording grouping and source text retained. Generated descriptions are not additional human votes. Keep behind supplied-audio human judgments. |

Receipts live in `local/datasets/discovery/research-20261101/`: OSF project/file
API responses, `smd-western.html`, `wcmed-smd-intersection-audit.json`, the SiTunes
pinned tree, six checked metadata/documentation files, `situnes/acquisition.json`
and `situnes/intersection-audit.json`. The latter records input feature-manifest
hashes and all candidate identities. Downloads were bounded metadata reads;
this investigation fitted no models and changed no production code.

Before any new fitting, freeze its source, comparisons, group exclusions and
stopping rules in a new protocol. Keep nonpilot Memo confirmation recordings
out of fitting, calibration and selection. The small direct-vibe cohort and
the #708 comparison cohort are now development evidence; repeated winners on
them require new independent confirmation. Native emotion or musical-property
gains justify further research, not automatic productionization.

### Completed follow-ups from the November 1 research audit

All five bounded studies above now have reproducible experiments-only results:

- #809: [MERT layers](MERT_LAYERS_RESULTS.md). Native gains on 16/24 targets,
  but all six mapped-vibe gates retain the incumbent specialists.
- #810: [chroma complement](CHROMA_RESULTS.md). Focus improves on the small
  reused activity cohort; #815 tracks independent confirmation and conditional
  production work. Five other vibes retain. The development gain is not measured
  against an independent production-model test set.
- #811: [mid-level supervision](MIDLEVEL_RESULTS.md). Official 5,000-item
  annotations acquired; 360 verified existing excerpts support a leakage-protected
  pilot. Predicted properties do not improve the downstream models reliably;
  true-property oracle gains motivate research rather than deployment.
- #812: [WCMED transfer](WCMED_RESULTS.md). Original audio recovered for 58
  sources; the repaired benchmark contains 285 distinct excerpts / 56 recordings /
  18 related groups. Useful activation ranking, with reconstruction and ordinal
  label limits; no new fitted model or production candidate.
- #813: [SiTunes feasibility](SITUNES_RESULTS.md). Full 1,406 paired records
  evaluated with participant, track and later-stage separation. Initial mood is
  the clear predictor; context/music additions are uncertain or regress. Existing
  audio covers just 39 paired records on 11 unverified tracks. Retain models.

Nonpilot Memo remains reserved. All artifacts and source snapshots are under
experiments/; none of these studies changes production inference.

### Focus chroma confirmation follow-up (#815)

[The larger protected comparison](FOCUS_CONFIRMATION_RESULTS.md) retains the
incumbent. On the primary 450-row comparison, adding chroma changes NDCG from
.497920 to .514290, paired interval [-.038792,.065672], while lift falls .007833
and native Reading/Studying errors rise slightly. The 494-row and outside-pilot
diagnostics do not recover reliable improvement. All accepted incumbent
predictions replay; the 32 shared chroma vectors reproduce exactly.

Fresh source recovery acquired HumMusQA's 320 human questions and 108-track
metadata with verified publisher MD5s. Its one studying/working answer shares
an artist with prior development data. Nineteen inventories identify 47
identity/artist overlaps; the other 61 tracks have no native activity labels.
PepMusic requires a request and combines studying with unrelated activities;
Music in Use's public file API returns 401; the recovered brain-focus repository
has session measurements but no stimulus audio. Original 7digital activity
references still lack verified recording/window alignment. The independent
confirmation gap remains explicit; there is no new deployment recommendation.
