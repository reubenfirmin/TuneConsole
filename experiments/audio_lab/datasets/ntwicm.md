# NTWICM — Now That's What I Call Music!

Source and annotation download: [NTWICM-Mood-Annotation.arff](http://openaudio.eu/NTWICM-Mood-Annotation.arff).

NTWICM contains 2,648 song annotation rows from the UK compilation series,
with artist/title identities and four raters' static whole-song valence/arousal
judgments. Each coordinate is one of −2, −1, 0, 1 or 2. The supplied partitions
contain 690 training, 686 development and 1,272 test rows. The ARFF file is
CP1252-encoded and does not include audio.

TuneConsole uses 55 existing recordings in 50 groups after deduplication and
identity checks. Audio is matched by normalized artist/title against other
acquired collections, and targets retain all four ratings and their disagreement.
Whole-song averages map to 0–1 for fitting.

These matches are weak song-level supervision on available excerpts. They do
not verify the original master, edit or rated section, and they do not reproduce
the original full-song benchmark. Shared recordings and artists must remain
connected across the source collections.
