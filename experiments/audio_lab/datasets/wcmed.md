# WCMED classical ranking

Nov 1, #812: recovered 58 exact Saarland source recordings (61 listed), covering
285 eligible excerpts from 56 recordings in 18 work/performer components.
The corpus includes piano, strings, orchestra, voice and chamber music.
Source terms are CC BY-NC-SA 3.0. Three Debussy downloads were unavailable;
different performances were not substituted.

The release's processed excerpts are absent. Center-ten-second reconstruction
uses supplied start/stop times; unspecified attack/decay processing prevents
sample-equivalence claims. Exclusions include two ambiguous metadata rows,
13 unavailable-source excerpts, 99 short intervals and one uncovered interval.
Round 3 fixes a repeated Chopin segment ID by excluding both ambiguous rows;
it supersedes round 2's 287-row evaluation after outcome exposure.

Rank 1 is highest. Published continuous values are exactly rank transforms,
not absolute VA ratings. No fitting or calibration occurred. Frozen DEAM prior
Spearman was .34880 valence / .69717 arousal. Adopted Uplift's rank NDCG gain
was +.20844 [.00303,.37489]; Energize's +.06627 [-.00471,.10466] was uncertain.
Cruise/Relax also require unverified cross-axis midpoint/distance proxies.
Retain all specialists: useful external association does not select a new model
or establish Focus, Release or mood-regulation benefits.

Evidence: `local/wcmed-812-20261101/round-3/`, with earlier rounds preserved.
Protocol: [WCMED](../WCMED_PLAN.md).
Verify with `./experiments/run.py wcmed-study verify`.
Sources: [release paper](https://arxiv.org/abs/2002.09021),
[original recording listing](https://www.audiolabs-erlangen.de/resources/MIR/SMD/western-music).
