# Relax: provenance

Selected Oct 30, 2026 (#629): #617's fixed consensus, with per-excerpt clipped
native prediction averaging. Its frozen research procedure passed advancement
thresholds on reused corpora; learned weights did not.

## Derivation

| Source | Retained rows | Native mapping | Weight |
| --- | ---: | --- | ---: |
| [Emotify](../datasets/emotify.md) | 400 | (calmness + 1 - tension) / 2 | .5 |
| [Soundtracks](../datasets/soundtracks.md) | 470 | (2 - energy - tension + tenderness) / 3 | .25 |
| [ManyMusic natural](../datasets/manymusic.md) | 188 | (1 - arousal + valence) / 2 | .0625 |
| ManyMusic generated | 95 | same VA map | .0625 |
| [Memo pilot](../datasets/memo2496.md) | 63 | same VA map | .125 |

Eleven ridge heads become five source matrices after verifying matching
preprocessing. Training uses recording mean embeddings; serving clips native
outputs separately per excerpt before averaging and applying fixed maps/weights.
The linear maps commute with prediction averaging, not with clipping inputs.

Artifact: [relax-vibes.json](../../../src/yt_playlist/rec/data/relax-vibes.json),
113,117 bytes, SHA256
`4fc43b28a71304ababa8514100f8080752f308a37f397681b127332febb5fbbd`.
Source `local/relax-617-20261030/round-1/models/full.json`, SHA256
`46ca8cc085adaab92eed16fa51b49ac4de311996a8b9492f144a5cf70aec7851`;
plan `c37bc4c30e8d42ae34d50a7f3aedebf898e61f61060dd01bc2caf2648abe3cf2`.
Retain adjacent source-terms notice; packaging establishes no new rights.

## Experiments and decisions

| Date / comparison | Outcome and reason |
| --- | --- |
| Oct 30, #579 | Emotify anchor promising, but single-source transfer/pilot gains uncertain; retain. |
| Oct 30, #617 | Fixed blend equal-corpus NDCG gain +.04310 over prior embedding, +.02887 over refitted anchor. Soundtracks anchor contrast +.04655 [.01541,.09373]; several other intervals include zero. Recommend fixed blend; learned weights fail transfer limits. |
| Oct 30, aggregation/adoption | Same-head aggregation diagnostic found negligible ranking changes. User selected per-excerpt prediction averaging; no refit or new confirmation was claimed. |
| Nov 1, #707–710/#809–812 | More windows, alternative representations and reserved Memo supplied no qualifying replacement. Classical rank transfer is useful but conditional on reconstructed audio/proxy labels. |

Positive valence supports VA experts without becoming a prerequisite for Relax.
Original local labels neither fitted nor selected consensus. Evidence:
`local/relax-617-20261030/round-1/` and `local/relax-production-20261030/`.
Protocol: [fixed/learned comparison](../RELAX_CONSENSUS_PLAN.md).
Verify: `./experiments/run.py relax verify`; export/runtime/package checks use
`relax-package`, `relax-production`, `relax-wheel`. See [encoders](encoders.md)
for shared representation failures and evidence paths.
