Author handoff for #616: the bounded experiment is complete. **Retain the current
production Energize scorer.** Independent acceptance remains pending.

Ran activation-only consensus, a fixed activation/energy/power/joy blend and a
cross-fitted exercise mapping; five fixed component controls; original-label
semantic diagnostics; omitted-corpus, browser and temporal comparisons. Five
outer fits, four omitted-corpus fits and one full research fit used 4,276 native
source rows. Primary comparisons use 1,227 common rows: Soundtracks 470, natural
ManyMusic 171, generated ManyMusic 95, Memo 63 and CAL500 428.

The fixed blend's browser NDCG is 0.87594/0.77513/0.90373/0.90587/0.33073 in
that order, versus production 0.84344/0.79242/0.90020/0.93122/0.25251. Its
Soundtracks ranking interval is positive, but its top-list interval includes
zero. Historical source-balanced gains are −0.016883 activation, +0.003544
consensus and −0.001807 exercise mapping, below the frozen +0.02 requirement.
Natural-music, Memo and named-control regressions also exceed the frozen limits.
No main candidate passes. Removing joy is the strongest diagnostic lead, but
historical natural/exercise ranking and Memo top-list losses remain.

Original CAL500 votes are sparse: 106/494 songs receive any exercise vote,
median three raters, split-half exercise correlation 0.116. Observed human
attributes predict exercise better than their audio-predicted counterparts
(0.46987 versus 0.30014 NDCG on the common 428 rows). The ranking-gap interval
includes zero; MSE improves. This shows useful semantic signal and prediction
limitations without identifying a unique failure cause or an accuracy ceiling.
The refreshed local cohort has 84 songs, 232 captures and 40 Energize positives;
no new identities or changed labels since #570. All populations remain inspected
development evidence, and unselected local vibes remain unknown.

Reproduction and verification:

```sh
./experiments/run.py individual-local --directory experiments/audio_lab/local/energize-616-20261030/local-1
./experiments/run.py --log audio_lab/local/energize-616-20261030/freeze.log energize freeze
./experiments/run.py --log audio_lab/local/energize-616-20261030/run.log energize run
./experiments/run.py energize verify
./experiments/run.py check --test test_energize_consensus.py
```

Receipt verification passed. **Six focused checks passed in 43.99 seconds**:
independent outer/transfer coefficient replay, independent primary metrics,
exact frozen references and identity-component isolation through both training
stages, alongside controlled unit cases. Supplemental read-only replay checked
5,986 saved native predictions and all 252 local excerpt cases (2,268 method
scores), maximum absolute error `1.5543122344752192e-15`.

Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0; one numerical
thread. Full research model: 150,152 bytes. Warm three-excerpt inference median
0.803 ms, maximum 0.918 ms, peak process RSS 653,266,944 bytes; all frozen
resource bounds pass. Timing excludes audio extraction, encoder and app startup.

Frozen directory: `experiments/audio_lab/local/energize-616-20261030/round-1/`.

| Evidence | SHA256 |
| --- | --- |
| `plan.json` | `0500878ae3cc15b57cb840b3e5c4f0723029c026da7938ef53d0ae523cb35d42` |
| `report.json` | `b0063a608fd13ed94bfb88ecc5769e3b6d48c1260fe225ccbfec57a278d20f68` |
| `predictions.npz` | `6aa5ae4dcb485283956576919be516e1e56429b9026d13a802ce7de5c4f6605b` |
| `evaluation.receipt.json` | `b021c0f0373cdef06bc67e367fdad4b86403ecb15b9b733d1ef6e5999134578c` |
| `models/full.json` | `0801f0919f21f9ca7bcc1ad35d7ec872bc60db0c74227ab9423cfd3e9266660e` |

Full results, uncertainty, transfer failures, calibration and coverage are in
`experiments/audio_lab/ENERGIZE_CONSENSUS_RESULTS.md`; native definitions and
original-vote diagnostics are in `ENERGIZE_LABEL_AUDIT.md`. These are retained
workspace artifacts, not published repository files. This comment reports the
author's experiment and independently calculated checks, not independent ticket
acceptance. The reviewer should assess the retained evidence and reproducibility.
No production change, full suite, global-suite lock, commit or push was needed.
