# Arousal-conditioned Uplift result, #712

**Retain the adopted model.** The frozen gate fails the primary ranking gains,
paired uncertainty and domain regression criteria. No production subtask is
warranted. This preserves #618's research retain result and the later user
decision to adopt its fixed consensus.

## Procedure and independence

The [frozen plan](UPLIFT_GATE_PLAN.md) fits one nine-pair regularized sigmoid
gate between valence and happiness/joy families. Arousal context comes from
native DEAM/PMEmo heads; dataset identity is absent from inference. The full
fit selects `(b0,b1)=(0,2)`; three outer fits select that pair and two select
`(1,2)`. All three corpus-omission fits select `(0,2)`. No cell-specific model
is adopted. These gates give valence greater weight at higher predicted arousal;
training did not select the intuitive opposite direction for gentle music.

The eight original sources contain **3,998 recordings**: DEAM 1,800, PMEmo 766,
Soundtracks 470, Jamendo 216, Emotify 400, natural ManyMusic 188, generated
ManyMusic 95 and original Memo 63. The original common evaluation mask remains
fixed, including 171 natural ManyMusic rows. New Memo recordings never enter.
Joining all sixteen source-version inventories (original thirteen plus three
pilots) and local identities adds **eight
component merges** to the old #618 partition, including indirect artist links.
Five outer folds were rebuilt. Every native/context head and every gate fitting
stage excludes complete validation components; all source-omission stages remove
connected components from all remaining sources. Native labels, head selection
procedure, within-family weights, chunk averaging and references stay fixed.
Head coefficients are refitted solely for these stronger exclusions.

The matched fixed, constant-weight and DEAM controls use the same repaired heads.
The constant weights were frozen earlier: DEAM .5, Soundtracks .25, Emotify .25.
The exact adopted full artifact, exact frozen full constant artifact and cached
prior DEAM anchor are also replayed separately. Their prior fitting/evaluation
exposure prevents treating them as independent out-of-fold controls. Unknown
artist names/aliases remain unresolved where source metadata lacks them; grouped
evaluation protects known identities and cannot establish unknown-artist isolation.

## Fixed-reference results

NDCG, five-fold macro average; historical / browser preparation:

| Cohort | Gate | Matched fixed | Matched constant | Exact adopted |
|---|---:|---:|---:|---:|
| Soundtracks | .69966 / .71144 | .75466 / .77098 | .66965 / .67931 | .76715 / .77394 |
| Natural ManyMusic | .79875 / .80273 | .78765 / .78699 | .81137 / .82193 | .79276 / .80430 |
| Generated ManyMusic | .86365 / .87268 | .86160 / .86563 | .88990 / .87933 | .87128 / .87036 |
| Original Memo | .94607 / .96736 | .97031 / .96522 | .93349 / .93008 | .97144 / .96522 |

Primary source-macro gate-minus-fixed NDCG is **-.02422 / -.01534**, and
top-list lift **-.02051 / -.01493**. Versus matched constant it is
**+.00772 / +.01883** NDCG and **+.01171 / +.02040** lift. Neither preparation
meets the joint .02 NDCG / .01 lift gain rule. No primary paired NDCG lower bound
is positive against either matched control.

Selected gate-minus-fixed paired 95% component intervals, 1,000 draws:

| Domain/preparation | NDCG change | 95% interval | Lift change |
|---|---:|---:|---:|
| Soundtracks historical | -.05500 | [-.08741, .00393] | -.04939 |
| Soundtracks browser | -.05954 | [-.09847, -.00266] | -.05397 |
| Natural historical | +.01110 | [-.00691, .06406] | +.00308 |
| Natural browser | +.01574 | [-.01258, .05182] | +.00465 |
| Memo historical | -.02423 | [-.04194, .01086] | -.01314 |
| Memo browser | +.00214 | [-.03037, .01697] | +.00644 |

Whole-corpus omission does not resolve the tradeoff. Gate-minus-fixed NDCG is
Soundtracks **-.00955 / -.01742**, natural **+.00068 / +.02321**, generated
**+.00472 / -.00473**, Memo **-.02223 / -.00408**. Corresponding lift changes:
**-.00945 / -.01945**, **-.00155 / +.01818**, **+.00438 / -.00099**,
**-.01314 / .00000**. Primary and transfer regression limits fail.

Mapped historical MSE gate/fixed: Soundtracks **.03734/.03457**, natural
**.03971/.04168**, generated **.01818/.01869**, Memo **.02166/.02563**.
The Memo error improvement accompanies worse historical ranking: calibration
and list quality are distinct. Native predictions are identical between mixers;
historical native MSE is DEAM valence .01027, PMEmo valence .01254,
Soundtracks valence/happy .02520/.03659, Jamendo happy .11626, Emotify joy
.03583, natural valence .03819, generated valence .01336, Memo valence .00784.
All native MAE, browser errors, mapped calibration, absolute metrics and paired
intervals for every control/domain are preserved in `report.json`.

## Contrasts and meaning

Genuine Soundtracks gentle-positive support is **one recording / one component**:
unsupported, with no accuracy claim. Its energetic-negative slice has 32/27;
gate/fixed mapped MSE **.07087/.06018**. VA-only gentle-positive proxies have
DEAM **15/15**, PMEmo **3/3**, natural **26/25**, generated **5/5**, Memo **1/1**.
Generated and Memo energetic-negative slices are empty and remain unsupported.
Only DEAM and natural proxies meet the predeclared 15-row/10-component threshold.

DEAM's proxy improves NDCG **.88850 to .91368** and lift **-.00333 to .02583**;
natural improves **.85064 to .86914** and lift **.02724 to .04308**. These are
positive-valence/arousal proxies, with no observed happiness or listening-benefit
labels. Their improvement cannot overcome the failed domain criteria or confirm
gentle-positive happiness coverage. Full contrast IDs/support are saved, including
empty and underpowered slices. All populations were previously inspected;
results are exploratory follow-up evidence.

## Evidence and checks

Final directory: `local/uplift-gate-712-20261101/round-2/`. Plan SHA256:
`24bc1c737fbc3e69702a2dfcd443915ca2a972245c0691891d54fda9553074c6`.
Ten receipts verify models, fit traces, predictions, resources and report. Round 1
preserves completed fits and an interrupted evaluation; before inspecting its
outcomes, round 2 added exact full constant replay without changing any gate or
decision rule. That earlier compute is additional to the final timing.

Final fits **29.07 seconds**, evaluation **92.25 seconds**, recorded peak RSS
**560,680,960 bytes**; full artifact **181,145 bytes**. Warm three-chunk replay
median/max **.970/1.146 ms**, independent replay maximum error **0**. All resource
limits pass. Python 3.14.8 / NumPy 2.4.6; complete runtime versions are frozen.

```sh
./experiments/run.py uplift-gate freeze
./experiments/run.py uplift-gate run
./experiments/run.py uplift-gate verify
./experiments/run.py check --test test_uplift_gate.py
./experiments/run.py investigate uplift_gate_712.py -k 'not receipts' -s
./experiments/run.py investigate uplift_gate_712.py -k receipts
```

Use a new `--directory` for reproduction. Four focused checks and four actual-data
checks pass: convexity/exact fixed recovery, cross-fitting, complete component
repair, every stage's exclusions, unchanged native truth, support rules and
receipt integrity. No production edits, global-suite lock or new listening labels.

Source-provenance correction found during #708: the original freezer captured
imported helpers but omitted this CLI's `__main__` entrypoint. The current
`uplift_gate_experiment.py` is separately preserved under `postrun-source/`,
with its hash, capture time and original plan hash in `postrun-source.json`.
This is post-run reproduction code, not a prospective entrypoint hash. Original
plans, archived helpers, inputs and measured outputs remain unchanged. The
freezer is fixed for future rounds. Verification now covers eleven receipts,
including the additional post-run source record; the source-freeze gap remains
an explicit limitation for independent review.
