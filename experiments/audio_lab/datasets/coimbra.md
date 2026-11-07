# Coimbra MIREX-like mood dataset

Publisher: [University of Coimbra music information retrieval group](https://mir.dei.uc.pt/).
Acquired audio and annotations: [MIREX-like release mirror](https://github.com/ethanray123/AudioEmotionPredictionModel/tree/8bdd6492f520d76cc2fa74ff69885b2b2b9c5777/Audio%20and%20Lyric%20Sentiment%20Neural%20Network%20Regression/MIREX-like_mood).

This collection contains 903 music clips assigned to five mood clusters derived
from AllMusic tags: rousing, cheerful, brooding, quirky and aggressive. Each clip
has one cluster assignment, recording metadata and a numbered audio filename.
These are tag-derived categories, rather than newly collected listener ratings
or continuous valence/arousal measurements.

TuneConsole uses the supplied 903 clips; protected per-vibe comparisons retain
900. Numbered filenames join to row-aligned cluster annotations. The semicolon
metadata requires UTF-16 decoding and one documented repair of an unquoted
semicolon in the title for `782.mp3`. The original collection has 668 artist
groups.

This release is distinct from the original MIREX benchmark and the
[4Q Emotion Dataset](4q.md). Cluster predictions do not share an absolute scale
with valence, arousal or product intensity scores.
