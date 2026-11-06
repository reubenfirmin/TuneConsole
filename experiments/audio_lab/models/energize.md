# Energize: provenance

Selected Oct 30, 2026 (#631): #616's declared no-joy diagnostic, chosen explicitly
for broader labeled-source coverage with its recorded losses. The research
recommendation remains **retain**; coverage is not proof of a general upgrade.

## Derivation

| Source | Retained rows | Native attribute | Weight |
| --- | ---: | --- | ---: |
| [DEAM](../datasets/deam.md) | 1,800 | arousal | .125 |
| [PMEmo](../datasets/pmemo.md) | 766 | arousal | .125 |
| [Soundtracks](../datasets/soundtracks.md) | 470 | energy | .25 |
| [Emotify](../datasets/emotify.md) | 400 | power | .25 |
| [ManyMusic natural](../datasets/manymusic.md) | 188 | arousal | .0625 |
| ManyMusic generated | 95 | arousal | .0625 |
| [Memo pilot](../datasets/memo2496.md) | 63 | arousal | .125 |

Seven ridge native heads use 3,782 source rows, with overlapping identities.
Source-balanced arousal totals 50%; Soundtracks energy and Emotify power each
25%. Joy, CAL500 and exercise mappings are absent. Train on mean embeddings;
serve clipped excerpt predictions averaged before fixed weighting.

Artifact: [energize-vibes.json](../../../src/yt_playlist/rec/data/energize-vibes.json),
119,514 bytes, SHA256
`ee27a94000a37b0252ca9f3b580e727268ce9d04fb8a28d9341b7263379cdc20`.
Source `local/energize-616-20261030/round-1/models/full.json`, SHA256
`0801f0919f21f9ca7bcc1ad35d7ec872bc60db0c74227ab9423cfd3e9266660e`;
plan `0500878ae3cc15b57cb840b3e5c4f0723029c026da7938ef53d0ae523cb35d42`.
Adjacent notice retains source terms. No refit occurred during export.

## Experiments and decisions

| Date / comparison | Outcome and reason |
| --- | --- |
| Oct 30, #578 | Generated-music gains failed consistent transfer; retain. |
| Oct 30, #616 | Activation, consensus with joy and learned exercise mapping all failed aggregate/domain gates. Direct exercise prediction ranked its native task well but transferred poorly; adding activity targets did not solve broad activation. |
| Oct 30, no-joy adoption | Browser shared/no-joy NDCG: Soundtracks .84344/.85145, natural .79242/.79176, generated .90020/.93017, Memo .93122/.92397, exercise .25251/.27286. Memo lift fell .017; historical natural/exercise losses remained. User accepted these tradeoffs. |
| Nov 1, #707/#708/#710/#809–812 | More windows, encoders and predicted properties did not qualify a replacement. Reserved Memo triggered regression limits; classical activation transfer is encouraging association, not new model selection. |

Radio intensity keeps **shared** Energize and Release channels under
`intensity_fits`; independent specialist scores rank within each vibe.
Evidence: `local/energize-616-20261030/round-1/` and
`local/energize-production-20261030/`.
Protocol: [consensus](../ENERGIZE_CONSENSUS_PLAN.md).
Verify: `./experiments/run.py energize verify`; package/runtime checks use
`energize-package`, `energize-production`, `energize-wheel`.
