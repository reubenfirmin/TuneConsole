Parent: #530. Priority: **P2**. **In review**. Owns the original Lift my mood /
Uplift comparison; follow #530's shared protocol and reuse #570/#571/#572 and #424.

## Completed research and follow-up

The original bounded experiment is complete and awaits independent acceptance.
Author evidence: `experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md`, Uplift,
calibration, distinctions and artifact sections; frozen run
`local/individual-20261030/remaining-1/`.
Its decision remains retain: DEAM improved natural-music ranking from 0.7084 to
0.8191 but regressed on Soundtracks from 0.7318 to 0.5533. Equal VA averaging
did not resolve the failure. #618 owns the authorized mapping and consensus
follow-up; new results must not rewrite this original comparison.

## Definition and controls

Positive valence is the declared external suitability reference; it does not
prove a causal change in a listener's mood. Primary sources are PMEmo/DEAM
human valence, Emotify joyful activation and Soundtracks happiness/valence.
Keep the prior PMEmo embedding ridge as a declared control. Jamendo happy votes
are a separate task; MERGE positive quadrants and Coimbra cheerful classes are
weaker proxies.

Browser-prepared shared NDCG controls on natural/generated ManyMusic, Memo
and Soundtracks: 0.6835/0.9395/0.9258/0.7513.

## Work

1. Freeze bounded embedding/combined heads, acoustic/constants controls and a
   predeclared equal VA ensemble. Use grouped native OOF and source-only-selected
   cross-corpus evaluation, excluding complete known artist/recording/audio
   components before selection.
2. Compare fixed mapped predictions/references with exact frozen/refreshed
   product on identical ManyMusic natural/generated and Memo rows. Keep native
   emotion tasks, historical/browser preparation and previously inspected
   exposure explicit. Do not fit mixtures or choose sources using target labels.
3. Report ranking, top-list suitability/lift, MSE, calibration drift, domain
   failures and paired uncertainty.
4. Use supported annotated/local cases for gentle joy, energetic unhappy music,
   aggression and changing excerpts. Uplift must not collapse into Energize;
   unsupported distinctions remain unknown.

Deliver reproducible exact-baseline results, an explicit adopt/retain decision,
and a replay-verified artifact plus calibration/bounded inference/resource
evidence if supported. Research stays under `experiments/` through
`./experiments/run.py`; #573/#587 own production/output checks. No new listening
packet, causal mood-improvement claim or shadow rollout is required.
