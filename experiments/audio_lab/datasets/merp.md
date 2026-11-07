# MERP — Music Emotion Recognition with Profile Information

Source, audio and annotations: [publisher's Hugging Face release](https://huggingface.co/datasets/amaai-lab/MERP/tree/04d90b60d92f079f2e25759e48f233094f7af84a).
Study and processing code: [author repository](https://github.com/dorienh/MERP).

MERP supplies 54 full-length WAV recordings with time-varying valence/arousal
ratings: 50 Free Music Archive songs and four DEAM quality-control anchors.
The filtered annotations contain responses from 277 raters, with nominal −1 to 1
coordinates. Raw and filtered Parquet tables join to audio by song ID and retain
rater IDs. Listener profiles are a separate part of the release and are not
TuneConsole model inputs.

TuneConsole excludes the four DEAM anchors and uses 50 songs in 43 artist
groups. Repeated trials are averaged within listener/song before averaging
listeners. The static targets average each annotation timeline and are paired
with center excerpts, so they are weak whole-song supervision.

Exact temporal alignment remains unverified: the README describes 10-Hz
annotations, while author processing resamples to half-second steps and removes
the start. The acquired files are checked against the pinned Git LFS SHA256
or Git blob SHA1 and byte count. The release card lists CC BY 4.0.
