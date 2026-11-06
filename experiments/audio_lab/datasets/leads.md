# Dataset leads: acquisition and rejection log

These are dated Oct 29–Nov 1, 2026 access/metadata findings, not current service
availability or newly measured model results. No broad recording recovery or
access requests were performed. Promote a lead to its own dataset log when a
bounded acquired corpus supports an experiment. Preserve source terms, native
label meaning, recording/window identity and connected overlap before fitting.

## Supplied-audio and semantic leads

| Lead | Evidence and disposition |
| --- | --- |
| Song Describer | MD5-verified CSV: 1,106 captions / 706 recordings; validated flags give 746 captions / 547 recordings (card says 546). Exact Jamendo artist/track exclusions leave 246 without known overlap. No audio acquired. Keep evaluation-only, per-recording terms; missing words are not negative labels. |
| EMID | Revision `c414773bbe9531cd13c039c40c1a554d9317ef47`: 10,738 filenames but 1,836 originals; 8,902 augmented rows repeat human bundles. Original clips 3.7–9.4 s cannot satisfy ten-second input without a new short-clip protocol. No audio acquired; simple parent-name joins miss many augmentations. |
| MusAV | Human pairwise activation/positivity for 2,092 previews; archived audio requires approval. No request sent. Useful ranking lead once access exists. |
| GlobalMood | Revision `975ec1ece254adf293e25eb304591dd5688b13d3`: 1,180 YouTube IDs, 117,996 native rating rows from five participant populations. One unverified existing-audio song match; no useful immediate benchmark or recovery project. |
| MuChin | V2 6,066-song audio archive gated and 26 GB; v1 has 724 overlaps. No corpus acquired. Noncommercial training restrictions and professional/amateur task distinctions remain. |
| PHENICX-emotion | Fifteen excerpts from one Beethoven symphony, academic terms. Human label files not established; narrow piece coverage. Later diagnostic only. |
| CP-WTC | 288 first-eight-bar excerpts of 48 Bach pieces / six pianists, 7,652 human annotations and released features; commercial audio excluded. Useful feature-level diagnostic, unavailable to current waveform extractor. |
| MuMeDB v2 | Advertised 1,412 multimodal records; file-list retrieval failed. Human provenance, overlap and usable supplied audio unverified. |
| MusicSem | Reddit-derived language with LLM processing and track IDs; no packaged audio, private human-validated test. Possible weak retrieval task, not new human votes. |

## Activity and mood-regulation references

| Lead | Evidence and disposition |
| --- | --- |
| HumMusQA (#815) | MD5-verified 320 questions / 108-track metadata. One studying/working answer shares an artist with prior ManyMusic development. Forty-seven tracks have known identity/artist overlap; remaining 61 lack native activity labels. No eligible rankable confirmation set or audio acquisition. |
| PepMusic | Requires data-request form; none submitted. Studying is bundled with dining/commuting/driving/breakfast; playlist-derived associations are weak targets. |
| Music in Use | Activity corpus uses title-derived categories and YouTube IDs. Derail task concerns transcription distraction; OSF file API returned 401. No supplied aligned audio established. |
| Brain-focus | Pinned five-file tree `43a5c5e76a2b7c65490ee0d88347cf4fcd44ddb9`: 51 participants' session-level focus over four streams; no stimulus audio. Session/decoded outcomes are not per-recording human votes. |
| Music and Cognitive Performance | Restricted 41-participant / 17-stimulus release; code tree has no conventional audio. Task-level treatment outcomes are not per-song suitability labels. |
| music&reading:Data1 | Two fNIRS spreadsheets; no supplied per-recording suitability/audio benchmark established. |
| Playing emotional music | Bounded metadata audit joins 59 label rows to 56 originals across eleven performers; three labels lack audio. Intended, communicated and performer-felt emotion are separate. The 168 split WAVs are dependent; no audio acquired or durations verified. |
| MUSMET VR | One musician / eight pieces / four conditions, video and EEG; independent listener ratings/standalone audio unestablished. Defer. |
| Extreme Metal and Anger Processing | Self-selected music versus silence after anger induction, 39 listeners. Song identities do not provide participant-to-recording outcome labels or cathartic suitability ground truth. Defer track recovery. |

Acquired [Moods and Activities](moods-and-activities.md) and
[Help Me study!](help-study.md) retain their own annotation experiments;
[SiTunes](situnes.md) retains the pre/post feasibility result. None establishes
the missing independently labeled Focus or cathartic Release audio benchmark.

## Unrecovered catalogue releases

Original MIREX remains separate from Coimbra/4Q; Yang-Dim remains a potential
cross-repertoire VA source; MoodSwings and MoodSwings Turk require distinct
versions and time alignment; DEAP's video ratings require an audio-validity
audit; CH818 was listed offline. Moodo's third-party XML supplies 7,186 responses
on 200 songs but lacks verified audio identities and publisher checksum
authenticity. Keep induced/perceived emotion, placements and preferences separate.
None enters current model derivations as an acquired independent audio corpus.

Evidence: `local/datasets/discovery/`, particularly `research-20261101/`,
`focus-576/`, and `local/focus-confirmation-815-20261101/sources/`.
Use `./experiments/run.py leads --help` for metadata acquisition/audits;
`focus-references` and `focus-confirmation audit-sources` reproduce activity
source checks from retained inputs. Detailed URLs, revisions, checksums and
failed requests remain in those artifacts.
