# Human mid-level musical properties

Nov 1, #811: publisher archive SHA256
`f0a36e901ed6d9e4f21d61d49cd2631642140e3329bb7e41c7524e09688fe1ac`
contains 5,000 averaged rows. Source IDs include 360 existing Soundtracks,
2,746 Jamendo, 829 Magnatune, 903 Multimodal and 162 Bimodal items. Only the
360 existing Soundtracks excerpts entered this pilot; no new audio acquired.

Seven native properties (melody, articulation, rhythm complexity/stability,
dissonance, atonality, mode) use **1–10**, not 1–9. The scale error was rejected
before fitting. Forty-five transitive film/audio components protect all nested
auxiliary and emotion stages. Composer metadata remains incomplete.

All properties beat training-mean error, but predicted-property bottlenecks
worsened all eight emotion errors. Concatenation improved only two errors and
no vibe advancement gate; its largest mean NDCG gain was Uplift +.00729 with
an interval crossing zero. True human properties improved six errors and
oracle Uplift NDCG +.16390 [.08565,.23644]; those inputs are unavailable at
inference. Discard these predicted-property heads as production candidates.

Evidence: `local/midlevel-811-20261101/round-1/` and `pilot-1/`.
Protocols: [acquisition](../MIDLEVEL_PLAN.md), [pilot](../MIDLEVEL_PILOT_PLAN.md).
Verify with `./experiments/run.py midlevel-study verify` and `midlevel-pilot verify`.
The archive README supplies no separate annotation licence; public access is
not redistribution permission. No pretrained property weights were used.
