# Human mid-level musical properties

Source and downloads: [Aljanaki and Soleymani's OSF dataset](https://osf.io/5aupt/).
Annotations and metadata: [metadata_annotations.zip](https://osf.io/download/4v3xh/).
Definition: [dataset paper](https://arxiv.org/abs/1806.04903).

The dataset contains 5,000 music excerpts with seven musician-rated perceptual
properties: melodiousness, articulation, rhythmic complexity, rhythmic stability,
dissonance, atonality and mode. The released averaged ratings use a 1–10 scale.
Preserve the literal atonality direction instead of relabeling it as stability.
The publisher supplies metadata/annotations and a separate audio archive.

The annotation rows comprise 360 Soundtracks, 2,746 Jamendo, 829 Magnatune,
903 Multimodal and 162 Bimodal items. TuneConsole uses only the 360 already
acquired Soundtracks excerpts, joined by source ID. These are reused recordings,
rather than 360 independent new emotion examples. Their evaluation grouping
contains 45 connected film/audio components; composer metadata is incomplete.

Property judgments cover the supplied excerpts, while TuneConsole features use
center ten-second inputs. The annotation archive SHA256 is
`f0a36e901ed6d9e4f21d61d49cd2631642140e3329bb7e41c7524e09688fe1ac`.
Its README gives no separate annotation redistribution licence.
