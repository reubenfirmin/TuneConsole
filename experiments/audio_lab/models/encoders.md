# Audio representation provenance and rejected alternatives

Current specialists use frozen 200-value MusiCNN `model/dense/BiasAdd`
embeddings (`musicnn-embedding-1`). The browser graph restores the signed
pre-activation readout in memory; native tags keep their original path. Average
full nonoverlapping 187-frame patch embeddings, preserving excerpt positions
and extractor/graph/weight identity. A ten-second input supplies 8.992 model
seconds. This is distinct from the published wrapper's overlap policy.

Oct 25: the original dynamic-code-generation Essentia runtime failed MV3 CSP.
Reject that runtime. The pinned Essentia/KISS FFT Emscripten build uses
`DYNAMIC_EXECUTION=0`; later production binding adds rhythm/loudness. Numerical
parity and resource scopes are in the [pipeline log](../BROWSER_EVALUATION_424.md)
and the preserved hashed preprocessing report.

## Matched representation studies, Nov 1, 2026

The #708/#809/#810 comparison shares 288 recordings, 265 known components and
576 exact ten-second intervals: 32 recordings each from nine source strata.
Five outer/three inner component folds, native targets, source omissions and
fixed vibe mappings remain paired. Nonpilot Memo is excluded. Exact shipped
artifacts are exposure-biased diagnostics; matched nested MusiCNN controls
establish representation contrasts, not independent full-product quality.

| Study | Recipe | Outcome and decision |
| --- | --- | --- |
| #708 | MERT-v1-95M final-layer temporal mean, 768 values | Focus point gain uncertain; Cruise mean gain lost on generated music and omitted-Memo transfer. All six advancement gates retain incumbents. |
| #809 | Concatenated temporal means of MERT hidden states 5/6, 1,536 values | Native MSE improved 16/24 targets versus final MERT, but every mapped-vibe gate failed. Release's Soundtracks transfer gain +.25628 [-.06822,.56992] did not improve native Jamendo ranking. |
| #810 | Fixed 72-value CQT chroma summaries, alone or appended to MusiCNN | No qualifying non-Focus replacement. Small CAL500 Focus gain motivated #815; the larger full-recipe test failed, so no chroma deployment. |
| #811 | Seven predicted human mid-level properties, bottleneck or augmentation | Bottleneck worsened all emotion errors; augmentation missed every advancement gate. Human-property oracle is unavailable to inference. See [dataset log](../datasets/midlevel.md). |

MERT uses pinned local custom code/weights, 24-kHz normalized PCM, CPU Torch
2.6.0 / Transformers 4.38.2 / NumPy 1.26.4 / Python 3.12.13, one inference
thread. All 211 checkpoint tensors were independently verified despite the
weight-normalization rename warning. Intermediate extraction summed 2,627.74
seconds inference and peaked at 1,020.73 MiB RSS; interruptions/setup make this
different from total fresh-run cost. Chroma uses Librosa .11 / SciPy 1.14.1;
warm Python computation was about .053 seconds per ten-second interval.
Neither establishes browser feasibility or total application preparation cost.

Evidence and read-only verification:

| Evidence root under `local/` | `./experiments/run.py` command | Frozen protocol |
| --- | --- | --- |
| `encoder-comparison-708-20261101/round-3/` | `encoder-comparison verify` | [encoder](../ENCODER_COMPARISON_PLAN.md) |
| `mert-layers-809-20261101/round-2/` | `representation-study verify` | [layers](../MERT_LAYERS_PLAN.md) |
| `chroma-810-20261101/round-1/` | `chroma-study verify` | [chroma](../CHROMA_PLAN.md) |
| `midlevel-811-20261101/pilot-1/` | `midlevel-pilot verify` | [properties](../MIDLEVEL_PILOT_PLAN.md) |

Source-freeze correction: early November freezers omitted CLI modules executed
as `__main__`. Five rounds retain post-run source copies/receipts, not missing
prospective hashes. #708 prospectively archives its corrected fit/evaluation;
reused extraction retains its original source gap. #809 round 2 references
round 1's extraction after a verifier source change, before comparison outcomes.
Keep original plans, measured outputs and receipts unchanged. New fits need a
new frozen directory with exact code/runtime checks. Development intervals
condition on fitted predictions and omit repeated-study selection and unknown
backbone overlap.
