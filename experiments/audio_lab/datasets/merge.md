# MERGE — bimodal audio/lyrics emotion dataset

Source and downloads: [MERGE version 1.1.0 on Zenodo](https://zenodo.org/records/13939205).
Audio archive: [MERGE_Audio_Complete.zip](https://zenodo.org/records/13939205/files/MERGE_Audio_Complete.zip?download=1).

MERGE supplies audio, lyrics and paired audio/lyrics collections for static
music-emotion recognition. The complete audio collection contains 3,554 clips,
nominally thirty seconds each; the lyrics collection contains 2,568 items.
Audio metadata includes song, artist, AllMusic ID and quadrant, with separate
continuous valence/arousal values.

The four quadrant classes are manually validated assignments derived from
AllMusic mood tags. Continuous values are automatically mapped from tags and
normalized to 0–1; they are not independently collected listener VA ratings.
These are separate native tasks.

TuneConsole uses the complete audio collection, with 3,553 clips long enough
for extraction. `MT0002054074` is only 4.778667 seconds and is excluded without
padding. The audio archive matches publisher MD5
`167f7ba80ab8eca8eee5a8668498a419`.

MERGE expands and overlaps [4Q](4q.md). Do not count both versions as independent
sources. Artist, recording identity, repeated AllMusic IDs and identical audio
connect evaluation groups across collections.
