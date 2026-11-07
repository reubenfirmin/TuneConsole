# SiTunes

Source and data: [publisher repository at the acquired revision](https://github.com/JiayuLi-997/SiTunes_dataset/tree/aea53feca9ba092406b5b95a3976dba0b44c568f).
Schema: [dataset README](https://github.com/JiayuLi-997/SiTunes_dataset/blob/aea53feca9ba092406b5b95a3976dba0b44c568f/SiTunes/README.md).

SiTunes records situational music recommendation in a three-stage user study.
It contains 2,006 interactions with aligned participant/item IDs, timestamps,
listening durations, ratings and item metadata. Stages 2 and 3 add mood before
and after listening, environmental context and wearable measurements; stage 3
also records preference. Mood valence/arousal use −1 to 1, recommendation ratings
use a five-point scale, and preference uses 0–100. These targets are distinct.

TuneConsole uses 1,406 paired pre/post-mood observations from 30 participants
and 305 item groups. Stage 1's 600 observations lack initial mood. Missing
duration, context and item metadata remain missing; repeated participant/item
pairs retain their identities.

The existing-audio intersection has only 39 paired observations across 18 people
and eleven tracks, with unverified recording/window matches. No aligned audio
intervention corpus is established. Before/after association without an untreated
comparison does not identify a causal effect of listening.
