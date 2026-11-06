# Uplift: provenance

Selected Oct 30, 2026 (#630): #618's fixed consensus. The frozen research
decision remains **retain** because ManyMusic domain regression limits failed.
The later explicit product decision accepts that tradeoff.

## Derivation

Nine ridge heads across eight source matrices predict native positivity:

| Source | Retained rows | Native attribute | Weight |
| --- | ---: | --- | ---: |
| [DEAM](../datasets/deam.md) | 1,800 | valence | .125 |
| [PMEmo](../datasets/pmemo.md) | 766 | valence | .125 |
| [Soundtracks](../datasets/soundtracks.md) | 470 | mean valence/happiness | .25 |
| [Jamendo](../datasets/jamendo.md) | 216 | happy | .125 |
| [Emotify](../datasets/emotify.md) | 400 | joyful activation | .125 |
| [ManyMusic natural](../datasets/manymusic.md) | 188 | valence | .0625 |
| ManyMusic generated | 95 | valence | .0625 |
| [Memo pilot](../datasets/memo2496.md) | 63 | valence | .125 |

Training uses mean embeddings per record. Serving clips each excerpt's native
predictions, averages excerpts and applies fixed source weights. No learned
weights or local-label calibration entered the adopted export.

Artifact: [uplift-vibes.json](../../../src/yt_playlist/rec/data/uplift-vibes.json),
140,485 bytes, SHA256
`b29217c7f706c9a5eb0716c7cd8d175ec4a85e65f309806ef66351710e7c97af`.
Source `local/uplift-618-20261030/round-1/models/full.json`, SHA256
`a1f1087b75f844eef74be62b39d627df611be9c2edabdc99920cbb591342f737`;
plan `2bf73a9f8f1360f50173f2cb3311dd1f31f9584a0d51054749704e5f28bb5b66`.
Adjacent notice retains unresolved source terms.

## Experiments and decisions

| Date / comparison | Outcome and reason |
| --- | --- |
| Oct 30, #580 | Positive-valence transfer had a material domain failure; retain. |
| Oct 30, #618 | Fixed blend mean NDCG +.04249 over shared, but fixed and learned procedures failed domain limits. Browser shared/fixed NDCG: Soundtracks .64734/.71462, natural .70473/.76192, generated .91344/.89136, Memo .88963/.97344. |
| Oct 30, adoption | User chose fixed consensus, accepting generated-music regression. Aggregate gain does not erase domain losses or turn the research result into a pass. |
| Nov 1, #712 | Nine-pair arousal-conditioned sigmoid gate failed ranking, uncertainty and domain gates. Stronger cross-source exclusions added eight component merges; refitted controls remain distinct from exact exposed full-artifact replay. Discard gate. |
| Nov 1, #707/#708/#710/#809–812 | More windows, encoders and mid-level predictions supplied no replacement. Reserved Memo regressed; WCMED adds positive rank association with reconstruction limits. |

Evidence: `local/uplift-618-20261030/round-1/`,
`local/uplift-production-20261030/`, `local/uplift-gate-712-20261101/round-2/`.
Protocols: [consensus](../UPLIFT_CONSENSUS_PLAN.md), [gate](../UPLIFT_GATE_PLAN.md).
Verify: `./experiments/run.py uplift verify`, `uplift-gate verify`;
export/runtime/package checks use `uplift-package`, `uplift-production`, `uplift-wheel`.
