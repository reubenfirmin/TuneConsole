# Matched alternative encoder, #708

**Retain all six incumbents.** MERT-v1-95M does not meet the frozen advancement
rules. Focus has a large but uncertain point gain. Cruise clears the mean gain
thresholds but loses on generated music and omitted-Memo transfer. No future
productionization subtask is supported by this run.

Final directory: `local/encoder-comparison-708-20261101/round-3/`. Plan SHA256:
`6e7407c5c1ff364923422b5fe9d1d7092ba32e2ceef864f11a7c10950152656d`.
The [frozen protocol](ENCODER_COMPARISON_PLAN.md) defines cohort selection,
preprocessing, mappings, fixed mixtures, exclusions, budgets and decision rules.
The final `report.json` contains all **72 supported source/vibe/mode cases**,
24 native-target errors, all exact-current diagnostics, calibration, MAE/MSE,
and paired 1,000-draw component intervals. Two omitted-source cases are explicitly
unsupported: direct CAL500 Focus and direct Jamendo Release lose their sole
expert. No substitute expert was invented.

## Matched scope

All **288 selected recordings / 265 joined components** passed extraction:
32 per source, **576 exact ten-second intervals**, no rejection or backfill.
Sources: DEAM, PMEmo, Soundtracks, human Jamendo, Emotify, natural/generated
ManyMusic, old Memo pilot and CAL500. Source component counts respectively:
32, 29, 31, 27, 26, 32, 30, 32, 31. There are 157 rows without artist names.
The join includes all sixteen source-version inventories (original thirteen
plus three pilots) and protected local identities. No new #710 confirmation
record enters training, selection, calibration or evaluation here.

Both encoders receive the same original intervals: one excerpt for the six
established sources, all three chunks of each rated ManyMusic window, and six
chunks covering Memo [15,75). Native targets, annotation rows, components,
five outer folds and three inner folds remain fixed. Identical mean/ridge
selection uses training-fold normalization and unchanged mapping/mixture ratios.
Every outer-held component is excluded from every source before selection.
Source omissions remove the entire destination family and all its components
from remaining sources before selection; both ManyMusic strata are omitted
together. There are 26 fitted contexts, thirteen per representation.

Exact current production specialists are evaluated too, with unchanged
coefficients. They may have trained on these identities and are exposed
diagnostics. The matched nested MusiCNN heads provide the controlled encoder
comparison; these are not the full production training procedures.

## Primary outcomes

Differences below are MERT minus matched MusiCNN. The four VA-compatible vibes
average eight primary cases each: Soundtracks, natural/generated ManyMusic and
old Memo, each grouped OOF and source-omitted. Focus uses CAL500 OOF. Release
uses Jamendo OOF and source-omitted Soundtracks anger. These are means across
the declared cases; grouped OOF and a single external holdout use different
ranking units and are not interchangeable population estimates.

| Vibe | Primary cases | Mean NDCG difference | Mean top-lift difference | Mean mapped MSE difference | Decision |
|---|---:|---:|---:|---:|---|
| Cruise | 8 | +.021334 | +.021482 | -.001628 | Retain: domain losses and uncertainty |
| Relax | 8 | +.001230 | +.012796 | +.000536 | Retain: NDCG floor, losses and uncertainty |
| Uplift | 8 | -.020511 | -.002012 | +.005174 | Retain: losses and uncertainty |
| Energize | 8 | -.018569 | -.010488 | +.004651 | Retain: losses and uncertainty |
| Focus | 1 | +.364977 | +.112500 | +.001117 | Retain: uncertainty |
| Release | 2 | -.191108 | -.067351 | -.002493 | Retain: ranking losses and uncertainty |

Focus OOF NDCG is **.221671 MusiCNN / .586648 MERT / .568213 exact-current**.
Its paired MERT-minus-matched NDCG interval is **[-.250000, .519101]**; top-lift
interval **[-.055556, .177170]**. The point gain does not establish reliable
advancement. The exact-current score is an exposed diagnostic, not fresh evidence
that MERT beats the adopted Focus model.

Cruise's omitted-Soundtracks NDCG improves **.545948 to .717826**, difference
**+.171878**, interval **[.004137, .312776]**. However, generated-music OOF NDCG
loses **.027971**, omitted-generated loses **.059501**, and omitted-Memo loses
**.085362**. Their intervals include zero, but their point losses exceed the
predeclared .01 domain ceiling. Soundtracks OOF NDCG improves .108265 with
interval **[-.000031, .201223]**. A promising cell does not satisfy all primary
case requirements.

Release direct Jamendo NDCG falls **.824020 to .459125**, difference **-.364895**,
interval **[-.896682, .039107]**. Source-omitted Soundtracks loses .017320 NDCG
and .030536 top lift. Native anger prediction improving below does not establish
that a Jamendo-aggression Release head transfers better to Soundtracks anger.

## Native error

OOF MSE, with each native label kept separate. Full MAE and conditional paired
uncertainty are in `report.json`; no pooled cross-construct error is used for
the decision. MERT has lower point MSE on seven of twenty-four native targets.

| Source / native target | MusiCNN MSE | MERT MSE |
|---|---:|---:|
| DEAM valence | .028975 | .030664 |
| DEAM arousal | .017589 | .019138 |
| PMEmo valence | .010106 | .012519 |
| PMEmo arousal | .013424 | .015035 |
| Soundtracks valence | .051574 | .055305 |
| Soundtracks energy | .017044 | .032781 |
| Soundtracks tension | .042470 | .037615 |
| Soundtracks happy | .063179 | .070989 |
| Soundtracks tender | .049582 | .067567 |
| Soundtracks anger | .080487 | .041188 |
| Jamendo happy | .165050 | .162087 |
| Jamendo aggressive | .027204 | .033306 |
| Emotify calmness | .036764 | .041824 |
| Emotify tension | .028455 | .034760 |
| Emotify joyful activation | .060360 | .060491 |
| Emotify power | .035309 | .041229 |
| ManyMusic natural valence | .047784 | .040883 |
| ManyMusic natural arousal | .067979 | .040557 |
| ManyMusic generated valence | .014688 | .013535 |
| ManyMusic generated arousal | .021404 | .023730 |
| Old Memo valence | .008849 | .011796 |
| Old Memo arousal | .020039 | .020137 |
| CAL500 Reading | .021223 | .021024 |
| CAL500 Studying | .038227 | .048593 |

## Extraction, replay and deployment assessment

Official [MERT model card](https://huggingface.co/m-a-p/MERT-v1-95M), revision
`12af15fef9d0ac838c3f475bfbbf26d2060dd4f5`; verified published weight SHA256
`a2b8b747f72c06e0595aeae41ae5473f4364938c6b39b2c58be38c48e6bd3fcd`.
Weights **377,552,987 bytes**; all six downloaded assets **377,585,144 bytes**,
download **54.58 s**. Incumbent MusiCNN graph **3,197,999 bytes**. MERT uses
float32 CPU eval mode, one thread, 24 kHz mono and last-layer temporal mean,
768 values. Its private runtime is Python 3.12.13, Torch 2.6.0+cpu,
Transformers 4.38.2 and NumPy 1.26.4. The research coordinator uses Python
3.14.8 / NumPy 2.4.6; full runtime/package records and worker sources are pinned.

Measured full extraction **2,396.09 s**, including both model workers and decode.
MusiCNN decode/extraction calls **200.34 s**; MERT decode/extraction calls
**2,145.89 s**. MERT interval inference median/max **3.565 / 4.528 s**; model
load **2.338 s**. Three extra encoder replays take **4.162 / 3.833 / 3.974 s**,
maximum embedding error **0**. Largest child peak RSS **1,036,988,416 bytes**;
this is not measured aggregate RSS or a separate MusiCNN-only peak. The
MusiCNN worker's elapsed context time includes waiting for MERT.

Compressed feature matrix **2,533,926 bytes**, MusiCNN cache **3,670,112 bytes**,
MERT cache **12,526,000 bytes**. Private environment/checkpoint/cache logical
file-size sum **2,189,058,326 bytes**, including hard-linked copies where present.
All declared compute/storage bounds pass. Original extraction costs remain in
the final round; copying adds **.145 s** and zero encoder calls. The independent
fresh PCM audit adds **190.26 s**, separately recorded in `pcm-audit.json`.

Head fitting **19.03 s**, evaluation **81.70 s**; coordinator peak RSS
**560,680,960 bytes**. Largest fitted head pack **2,458,279 bytes**; all 26 packs
**36,983,702 bytes**. Three cached replays of all 288 rows, all native heads and
six mixtures have median **7.477 ms MusiCNN / 151.976 ms MERT**, numerical error
**0**. These batch measurements do not replace extraction latency or establish
single-record browser latency.

CPU extraction is feasible within the research bounds, with materially larger
checkpoint, features and inference costs. Browser MERT export, WASM/WebGPU,
capture-resampling parity, low-memory client operation and aggregate client RSS
were not measured. Current 200-value production heads cannot consume 768-value
features. Neither the quality evidence nor this deployment assessment supports
a production encoder replacement. No production artifact was exported or changed.

## Evidence and source limitations

All six actual-data investigations and four focused checks pass. Fresh MusiCNN
features match the original reference chunks across all nine sources with maximum
error **.0000123978**, below .0005. All 576 MERT PCM hashes and feature positions
match fresh decodes; matching 16 kHz PCM hashes are retained too. Exclusion checks
cover every fitted context, native inner folds and both-Many omissions. Temporary
PCM directories are absent.

Round 1 retains extraction under its original plan and receipts. The freezer
omitted the main `__main__` module, and an initially late-imported metric helper
was also absent. Round 2's reuse check stopped before copying/fitting because it
could not find the archived entrypoint. Neither early round fits or scores heads.
Round 3 fixes the freezer and prospectively archives the complete fitting and
evaluation entrypoint, metric helper and checks. Feature reuse validates original
receipts, identical inputs/cohort/workers/policies and the fresh decode/parity
audits before fitting. Original extraction entrypoint AST equivalence remains
unavailable and is not claimed. Original plans and outputs remain unchanged.

The final cleanup assertion initially matched retained `pcm-audit` report files.
It now checks matching directories only and passes; the revised investigation
source is separately retained under `postrun-source/` with a post-run receipt.
The original archived test remains unchanged. Six final receipts cover reused
extraction, numerical parity, PCM audit, predictions, evaluation and post-run
source provenance. Post-run source copies never retroactively establish a
missing prospective hash.

Commands used for this run follow; fitting/evaluation preceded the post-run
provenance utility and cleanup-assertion revision. The shared freezer's separate
real `python -m` regression check also passes (one test, 4.81 seconds).

```sh
./experiments/run.py encoder-assets metadata
./experiments/run.py encoder-assets download
./experiments/run.py encoder-assets environment
./experiments/run.py encoder-comparison verify
./experiments/run.py check --test test_encoder_models.py
./experiments/run.py investigate encoder_comparison_708.py -k 'feature_reuse_provenance or frozen_selection or musicnn_interval_parity or interval_pcm_and_mert_row_order'
./experiments/run.py encoder-comparison fit
./experiments/run.py encoder-comparison evaluate
./experiments/run.py investigate encoder_comparison_708.py -k 'all_selection_exclusions or completed_result_and_resources'
./experiments/run.py check --test test_quality_round.py
```

For reproduction, preserve existing evidence and choose a new `--directory` for
freeze/extract/fit/evaluate. Current-source execution must pass a new source
freeze; historical `verify` validates archived evidence. Use the pinned asset
revision, inputs and policies rather than selecting a new checkpoint or cohort.
Only the failed cleanup assertion was rerun after correction; unchanged passing
checks were not repeated for another report. This is author evidence for
independent review, using previously inspected populations and small subsets.
Unknown artist aliases, encoder pretraining overlap and conditional intervals
limit generalization. Label/activity/VA references do not establish listening
benefit. No live database, global-suite lock, new listening session, production
code edit, commit or push was used.
