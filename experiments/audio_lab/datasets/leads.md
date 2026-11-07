# Additional dataset sources

These sources are outside the acquired audio collections in the
[dataset index](README.md). Metadata, annotations or a published study alone do
not establish an aligned audio dataset. Access requirements below come from the
documented releases; they are not live availability checks.

## Music annotations and audio

| Source | Dataset definition | Limits for TuneConsole |
| --- | --- | --- |
| [Song Describer](https://zenodo.org/records/10072001) | 1,106 natural-language captions for 706 recordings, with validation flags, audio metadata and individual licences. | The validated CSV subset contains 746 captions on 547 recordings; 246 remain after known Jamendo artist/track exclusions. No audio acquired. Missing words are not negative labels. |
| [EMID](https://huggingface.co/datasets/ecnu-aigc/EMID/tree/c414773bbe9531cd13c039c40c1a554d9317ef47) | Human emotion annotations on 1,836 original short clips, expanded to 10,738 filenames through augmentation. | The 8,902 augmented rows repeat original human labels. Originals last 3.7–9.4 seconds, below the ten-second input requirement. |
| [MusAV](https://mtg.github.io/musav-dataset/) | Human pairwise activation/positivity judgments for 2,092 music previews. | [Archived audio](https://zenodo.org/records/7448344) requires access approval; no acquired audio corpus. |
| [GlobalMood](https://github.com/harin-git/GlobalMood/tree/975ec1ece254adf293e25eb304591dd5688b13d3) | 1,180 distinct YouTube IDs and 117,996 mean-rating rows across five participant populations and native emotion tags. | No supplied audio acquired. Participant populations and native tags remain distinct; the single existing-audio song match has unverified recording identity. |
| [MuChin v2](https://github.com/CarlWangChina/MuChin-V2-6066) | 6,066 songs with professional/amateur Chinese music descriptions and an advertised audio archive. | Gated 26 GB audio release; no corpus acquired. Version 1 shares 724 songs with v2. Source terms restrict commercial audio training. |
| [PHENICX-emotion](https://www.upf.edu/web/mtg/phenicx-emotion) | Fifteen excerpts from one Beethoven symphony, with supplied audio and descriptors for academic research. | Usable human-label files are unverified; the excerpts do not provide broad independent piece coverage. |
| [CP-WTC](https://github.com/CPJKU/The-CP-WTC-Dataset) | 288 first-eight-bar excerpts of 48 Bach pieces played by six pianists, with 7,652 human annotations and released features. | Commercial recording audio is excluded. Features cannot replace waveform inputs to the current extractor. |
| [MuMeDB v2](https://data.mendeley.com/datasets/9zdww6wnyx/2) | Publisher describes 1,412 multimodal music records. | Usable supplied audio, human-label provenance and recording overlap are unverified. This is not 1,412 acquired training tracks. |
| [MusicSem](https://huggingface.co/datasets/AMSRNA/MusicSem) | Reddit-derived music language and track IDs with LLM processing. | No packaged audio; the human-validated test set is private. Generated descriptions are not additional human votes. |
| [TROMPA-MER](https://github.com/juansgomez87/vis-mtg-mer) | Listener emotion annotations associated with music identities and Muziekweb previews. | Annotations acquired, but no usable preview corpus. Preference and familiarity remain separate from emotion targets. |
| [FME-24](https://github.com/rubycrocker/FME-24-dataset) | Temporal film-music emotion data with recording identifiers, excerpt times and supplied acoustic features. | Copyrighted audio is excluded. The matching recording and interval are required before waveform extraction. |

## Activities and listening outcomes

| Source | Dataset or study definition | Limits for TuneConsole |
| --- | --- | --- |
| [HumMusQA](https://zenodo.org/records/18462524) | 320 human music questions with metadata for 108 tracks. | Only one studying/working answer, with prior ManyMusic artist overlap. It is not a rankable activity-label cohort. |
| [PepMusic](https://social-dynamics.net/pepmusic/) | Playlist-derived music/activity associations, including a category combining studying, dining, commuting, driving and breakfast. | Release uses a data-request form. Playlist associations are weak supervision, rather than independent concentration outcomes. |
| [Music in Use: activity corpus](https://repository.tudelft.nl/file/File_4042adbe-223b-4deb-a363-12c02a429775) | 2,589 YouTube items with activity categories derived from titles. | Video IDs do not supply verified audio or human suitability labels. |
| [Music in Use: derail moments](https://osf.io/n3zmp/) | Distraction judgments made during transcription, with YouTube IDs and moment annotations. | No supplied aligned audio established. Temporal distraction differs from general Focus suitability. |
| [Brain-focus](https://github.com/arctop/Measuring-And-Modeling-The-Effect-of-Audio-On-Human-Focus-In-Everyday-Environments-Using-BCI/tree/43a5c5e76a2b7c65490ee0d88347cf4fcd44ddb9) | Session-level focus measurements from 51 participants listening to four streams. | No stimulus audio supplied. Session or decoded outcomes are not per-recording listener votes. |
| [Music and Cognitive Performance](https://zenodo.org/records/8421227), [later release](https://zenodo.org/records/14946483) | Task outcomes from 41 participants, with Spotify identities/features for 17 stimuli. | Restricted files; no acquired audio corpus. Task-level treatment outcomes cannot become per-song suitability labels. |
| [Individual musicians playing emotional music](https://zenodo.org/records/10396364) | Intended, communicated and performer-felt emotion across eleven musicians; 59 label rows, 56 matched original performances and 168 dependent split WAVs. | Three label rows lack audio; no audio acquired. Keep the three emotion constructs and performer dependence distinct. |
| [MUSMET VR](https://zenodo.org/records/18642655) | One musician performing eight pieces under four emotion conditions, with video and EEG. | Standalone audio and independent listener ratings are unverified; conditions are not independent performers. |
| [Extreme Metal Music and Anger Processing](https://www.frontiersin.org/journals/human-neuroscience/articles/10.3389/fnhum.2015.00272/full) | A study of 39 listeners comparing self-selected music with silence after anger induction. | The track list lacks participant-to-recording outcome labels. It supplies no per-track cathartic-suitability ground truth. |

Acquired annotation collections have separate definitions:
[Moods and Activities](moods-and-activities.md), [Help Me study!](help-study.md)
and [SiTunes](situnes.md).

## Other emotion collections

| Source | Dataset definition | Missing inputs or distinctions |
| --- | --- | --- |
| [Original MIREX mood benchmark](https://www.music-ir.org/mirex/wiki/2020:Audio_Classification_(Train/Test)_Tasks) | Five-cluster mood classification benchmark. | Original release not acquired; separate from Coimbra MIREX-like and 4Q. |
| [Yang-Dim](http://mac.citi.sinica.edu.tw/~yang/MER/taslp08/#Data) | 195 Western, Chinese and Japanese popular songs with listener valence/arousal ratings. | No verified replacement archive acquired. |
| [MoodSwings](https://www.met-lab.org/emotion-recognition) | Time-varying music-emotion annotations. | Original MoodSwings and later MoodSwings Turk are distinct releases; neither is an acquired aligned audio source here. |
| [DEAP](https://www.eecs.qmul.ac.uk/mmv/datasets/deap/) | Music-video emotion ratings with physiological measurements from 32 participants. | Video can influence the ratings. Audio validity and accessible source files remain unverified. |
| [Moodo](https://lgm.fri.uni-lj.si/research/mood-and-music/) | 7,186 responses on 200 songs, with ten induced- and fourteen perceived-emotion categories in the acquired third-party XML. | [Third-party copy](https://github.com/s-bose/Moods_dataset/tree/cee7a64671aa073a7d9b6cd5556210eb20682aaf) lacks verified audio identities and publisher-checksum authenticity. |

The [original acquisition catalogue](https://github.com/juansgomez87/datasets_emotion)
also names CH818, but no verified publisher download is documented. It is not
an acquired dataset.
