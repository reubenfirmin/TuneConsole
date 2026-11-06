# SiTunes

Pinned revision `aea53feca9ba092406b5b95a3976dba0b44c568f`; acquired files match
Git blobs. Stages 2/3 supply 1,406 paired pre/post-mood observations from 30
people and 305 item groups. Stage 1's 600 observations lack initial mood and
remain audit-only. Retain missing metadata, native [-1,1] VA and repeated pairs.

Nov 1, #813: participant-held-out, track-held-out and later-stage fits compare
initial mood, context, participant identity and supplied descriptors. Training-only
imputation, scaling and nested selection protect each split. Initial VA regression
improves new-participant MSE over unchanged mood by -.020111 valence and -.031779
arousal, with negative paired intervals. Context adds no reliable gain; music
descriptors give no reliable held-out gain and worsen later-stage valence by
.010205 [.001641,.019657]. No deployable head or audio intervention model selected.

The audio intersection is only 39 paired rows / 18 people / 11 tracks, with
unverified recordings/windows and duration mismatches or short previews. No
broad recovery is justified without verified audio and terms. Pre/post association
has no untreated comparison and does not identify a causal listening effect.

Evidence: `local/situnes-813-20261101/round-2/`; round 1 stopped before fitting
because missing metadata serialized as NaN. Protocol: [SiTunes](../SITUNES_PLAN.md).
Verify with `./experiments/run.py situnes-study verify`.
Source: [pinned publisher repository](https://github.com/JiayuLi-997/SiTunes_dataset/tree/aea53feca9ba092406b5b95a3976dba0b44c568f).
