# DEAM — Database for Emotional Analysis in Music

Source: [DEAM publisher page](https://cvml.unige.ch/databases/DEAM/).
Annotation download: [DEAM release mirror on Zenodo](https://zenodo.org/records/11400122).
Acquired audio: [recorded audio mirror revision](https://github.com/Akminar/Classifying-Emotionality-of-Music/tree/3db39c68bf69609b57cdc31dd46fa9f8fd24970f/data/MEMD_audio).

DEAM contains 1,802 music excerpts and full songs with crowdsourced static and
time-varying valence/arousal annotations. The release preserves individual
responses, song-level means and standard deviations, and changing emotion
trajectories. Static ratings use a 1–9 scale; dynamic values use −1 to 1.
Numeric song IDs join annotations to supplied audio.

TuneConsole extracted complete features for 1,801 recordings. ID `2048` fails
the exact ten-second measurement-frame requirement by one sample after timestamp
trimming. Protected specialist fits use 1,800 rows; the separate common-target
Cruise comparison uses 1,798 after its exclusions. These are subsets of DEAM,
rather than different dataset totals.

Artist metadata is mostly missing, so recording grouping cannot establish
unseen-artist performance. The feature bundle includes a DEAM-trained emotion
head; its outputs are excluded from DEAM native-task features and direct
pretrained controls.
