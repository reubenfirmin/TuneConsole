# WCMED — Western Classical Music Emotion Dataset

Source and annotation download: [CCMED/WCMED publisher release](https://www.metacreation.net/projects/ccmed_wcmed_soundscape/).
Original source recordings: [Saarland Music Data, Western Music collection](https://www.audiolabs-erlangen.de/resources/MIR/SMD/western-music).
Definition: [dataset paper](https://arxiv.org/abs/2002.09021).

WCMED contains 400 excerpts from 61 Western classical recordings, spanning
piano, strings, orchestra, voice and chamber music. Its companion CCMED contains
400 Chinese classical excerpts. Human pairwise comparisons produce global
valence/arousal rankings. Rank 1 is highest; supplied continuous values are
the transform `1 - 2 * (rank - 1) / 400`, rather than absolute emotion ratings.

The annotation archive supplies excerpt metadata and start/stop times but no
audio. TuneConsole recovered 58 exact original Saarland recordings and uses
285 eligible excerpts from 56 recordings in 18 work/performer components.
Exclusions account for two ambiguous metadata rows, 13 unavailable-source
excerpts, 99 short intervals and one uncovered interval. Different performances
are not substituted.

Inputs reconstruct center ten-second sections. Unspecified attack/decay
processing prevents equivalence with the original processed excerpts.
Saarland source recordings use CC BY-NC-SA 3.0. Ranking on these reconstructions
does not establish absolute calibration, concentration benefit or anger relief.
