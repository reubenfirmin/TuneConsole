# Human mid-level supervision, #811

**Retain the direct model.** All seven properties are predictable above
training-mean baselines, but neither predicted-property method passes a
vibe advancement gate. This bounded result does not establish that larger
property pretraining or joint neural training would fail.

The official OSF metadata download succeeded. The 318,090-byte archive
matches the publisher SHA256
`f0a36e901ed6d9e4f21d61d49cd2631642140e3329bb7e41c7524e09688fe1ac`.
It contains 5,000 averaged annotation rows and explicit source IDs, including
360 Soundtracks Set 1 excerpts already available locally. No new audio was
needed. Source metadata also list 2,746 Jamendo, 829 Magnatune, 903 Multimodal
and 162 Bimodal items; these have not entered this pilot.

All 360 original audio hashes and exact cached feature identities verified.
Their 200-value MusiCNN vectors match the original frozen input arrays
exactly. The seven property labels describe the supplied excerpts, whereas
the historical embedding represents their center ten seconds. Film, legacy
identity and audio-hash connections merge transitively into 45 components.
Composer identities remain incomplete, so this is not composer-independent
evaluation.

The annotation scale is 1–10, confirmed by the publisher CSV and
[the 2019 paper](https://archives.ismir.net/ismir2019/paper/000027.pdf).
Preparation rejected an initial 1–9 assumption before fitting; the corrected
pilot was then frozen as plan
`f03754642434696d93e2bcb1c74d41d3ec7df8dfed6c8ef2a373b58ace15b80a`.
Literal column names/directions are retained, including `atonality`.
The archive README does not establish a separate annotation license; no
redistribution permission is inferred. The two author repositories audited
contain no annotation mirror. No externally pretrained property weights
were used, avoiding possible Soundtracks pretraining exposure.

Five grouped outer folds and three inner folds compare a direct embedding,
seven predicted properties, their 207-value concatenation, and true human
properties as an unavailable oracle. Every emotion inner split recomputes
the entire auxiliary pipeline; each training representation is itself
component-cross-fitted. Eighty retained contexts record protected components
and all auxiliary training rows. All serialized heads replay exactly on every
outer held-out recording.

Artifacts are under `local/midlevel-811-20261101/round-1` (acquisition) and
`local/midlevel-811-20261101/pilot-1` (models/results). Acquisition took 9.76
seconds; fitting took 4.13 seconds, 534.71 MiB peak RSS and 6,318,528 bytes of
model outputs. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1,
threadpoolctl 3.7.0, one numerical thread. The model paths and predictions are
research artifacts, not deployable production packs.

## Results

Normalized property MSEs (recording averages, lower is better):

| Property | Training mean | Predicted |
| --- | ---: | ---: |
| Melody | .06199 | .03452 |
| Articulation | .05154 | .01456 |
| Rhythm complexity | .04469 | .03437 |
| Rhythm stability | .03303 | .02778 |
| Dissonance | .04776 | .02930 |
| Atonality | .03615 | .02767 |
| Mode | .03200 | .02692 |

The bottleneck worsens all eight emotion MSEs. Augmentation improves 2/8
(energy and happy), with small differences. The oracle improves 6/8, including
valence .01832 to .00929 and happy .04164 to .01835, but worsens energy and
anger. Native errors and conditional paired intervals remain in report.json.

Differences versus matched direct prediction, using mean fold fixed-mapping
NDCG/top-list lift/MSE:

| Vibe | Bottleneck NDCG | Augmented NDCG | Augmented lift | Augmented MSE |
| --- | ---: | ---: | ---: | ---: |
| Cruise | -.00392 | +.00184 | .00000 | +.00008 |
| Relax | -.03140 | +.00155 | +.00213 | +.00001 |
| Uplift | -.02517 | +.00729 | +.00124 | +.00053 |
| Energize | -.01282 | +.00144 | .00000 | -.00016 |
| Release | -.09642 | -.00624 | -.00790 | +.00021 |

Every augmented NDCG interval crosses zero; all gains miss the .02 gate.
The oracle Uplift gain is +.16390 NDCG, interval [.08565,.23644], +.11034
lift and -.01331 MSE. This identifies useful information in human properties,
not an available audio model. The gap supports further research on a stronger
property encoder trained using more of the acquired collection. It does not
support productionizing these heads. Focus has no truth in this cohort.

Evaluation took 61.16 seconds. Commands:

```sh
./experiments/run.py midlevel-study verify
./experiments/run.py midlevel-pilot verify
./experiments/run.py investigate midlevel_811.py
./experiments/run.py investigate midlevel_replay_811.py
```

One acquisition and two pilot receipts verify. Two nested-exclusion/cohort
checks and one full serialized replay check pass. For fresh fitting, freeze a
new pilot directory, then run fit/evaluate with that directory. The original
rounds preserve their executed source snapshots. No production code, live
database, additional Memo data or user listening collection was used.
