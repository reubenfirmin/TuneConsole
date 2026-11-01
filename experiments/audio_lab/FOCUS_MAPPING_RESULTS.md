# Focus mapping results (#615)

30 October 2026. Native-attribute consensus does not improve the direct
Reading/Studying model. The hybrid adds complexity without a demonstrated gain.
The direct model remains the useful production candidate: it improves on the
shared scorer, while essentially matching the refitted #576 direct procedure.
The frozen decision is **retain for all three new procedures**, because none
clears the required improvement over that existing research control. This is
not a finding that the shared production scorer performs equally well.

No Focus production code changed. Any subsequent adoption should preserve this
decision and the uncertainty, rather than relabeling the round as a pass.

## Design and evidence

The [frozen plan](FOCUS_MAPPING_PLAN.md) compares direct audio prediction, an
18-feature native-attribute map, and a 218-feature audio-plus-attribute hybrid.
Each predicts original Reading and Studying fractions separately; Focus is
their equal mean. Ten CAL500 native attributes and sixteen external native
heads provide the mapped features. The [label audit](FOCUS_LABEL_AUDIT.md)
records 4,276 source rows, but only 494 CAL500 songs supply activity supervision.

All tables use the same 450 common recordings. Five cross-source identity
components joined old CAL500 folds, so this round uses new grouped folds and
refits the exact #576 historical/browser procedures on them. Every held-out
component is removed from every source in outer fitting, map selection and
mapping-feature cross-fitting. The new direct procedure selects mean/ridge;
the #576 controls also allow their original small RFF candidates. All choices
use inner native MSE. No outer result chooses a feature, weight or export.

Historical and browser preparations remain separate. New procedures train on
historical embeddings. The matching #576 browser control trains on browser
embeddings. Archived predictions remain in the report, explicitly marked as
legacy evidence with weaker component isolation. Changing folds changes
fold-specific rankings even for an unchanged shared scorer; the older #576
table is not directly comparable to this one.

## Primary comparison

| Procedure | Historical NDCG | Browser NDCG | Browser top-list suitability | Browser lift | Browser MSE |
|---|---:|---:|---:|---:|---:|
| Direct audio | 0.4979 | 0.4986 | 0.2200 | 0.1180 | 0.018822 |
| Attribute consensus | 0.4454 | 0.4358 | 0.1854 | 0.0833 | 0.019463 |
| Hybrid | 0.4945 | 0.4939 | 0.2200 | 0.1180 | 0.018854 |
| Refitted #576, matching preparation | 0.4979 | 0.5019 | 0.2200 | 0.1180 | 0.018800 |
| Shared, matching preparation | 0.4012 | 0.3999 | 0.1812 | 0.0792 | 0.021049 |
| Training mean | 0.2284 | 0.2284 | 0.1020 | 0.0000 | 0.021727 |

NDCG measures ranking, not classification accuracy. Top lists contain up to ten
recordings per outer fold, with tied predictions averaged fairly. Suitability
is the original mean listener fraction in those lists. Reported MSE averages
outer-fold MSE; calibration and the additional overall MAE use pooled rows.

Browser paired differences from 1,000 component bootstrap draws within fixed
outer folds:

| Comparison | NDCG difference, conditional 95% interval | Top-list lift difference, interval |
|---|---|---|
| Direct versus shared | +0.0987 [0.0046, 0.2046] | +0.0388 [-0.0095, 0.0848] |
| Attributes versus shared | +0.0359 [-0.0416, 0.1370] | +0.0042 [-0.0275, 0.0525] |
| Hybrid versus shared | +0.0940 [0.0141, 0.1939] | +0.0388 [-0.0032, 0.0896] |
| Direct versus refitted #576 | -0.0033 [-0.0232, 0.0116] | 0.0000 [-0.0150, 0.0078] |
| Attributes versus direct | -0.0628 [-0.1468, 0.0316] | -0.0346 [-0.0690, 0.0217] |
| Hybrid versus direct | -0.0047 [-0.0364, 0.0329] | 0.0000 [-0.0117, 0.0217] |

Direct versus shared MSE improves by -0.002226, interval
[-0.003311, -0.001087]. Historical direct versus shared has NDCG difference
+0.0967 [0.0116, 0.1986], with lift +0.0358 [-0.0062, 0.0821]. Thus the direct
scorer has useful development evidence over production in both preparations;
the size of the top-list gain remains uncertain. These are previously inspected
recordings, and intervals condition on the fitted procedures. They are not
fresh confirmation or a correction for all historical model research.

The direct candidate fails only the frozen +0.01 browser NDCG requirement over
the refitted #576 control. The hybrid also fails the incremental +0.01
requirement over direct. Attributes fail those requirements, the positive
interval requirement over shared, and the declared Focus/native regression
limits against #576 in both preparations. No resource limit causes a rejection.

## Separate activities and semantic diagnosis

| Browser procedure | Reading NDCG | Studying NDCG | Reading MSE | Studying MSE |
|---|---:|---:|---:|---:|
| Direct | 0.4690 | 0.4145 | 0.025495 | 0.032831 |
| Attributes | 0.3767 | 0.3194 | 0.025867 | 0.033826 |
| Hybrid | 0.4664 | 0.3908 | 0.025566 | 0.032864 |
| Refitted #576 browser | 0.4746 | 0.3909 | 0.025485 | 0.032773 |
| Observed human attributes | 0.3955 | 0.4435 | 0.025626 | 0.031838 |

Using the ten **observed** CAL500 attributes gives Focus NDCG 0.5125 and MSE
0.018503. Predicting those same ten attributes from browser audio gives 0.4345
and 0.019451. The observed-minus-predicted NDCG difference is +0.0779
[-0.0586, 0.1903]; MSE difference is -0.000948 [-0.001970, 0.000147]. Studying
shows a clearer prediction-loss signal in MSE: -0.001871
[-0.003635, -0.000103]. Reading does not show the same observed-attribute ranking
advantage. These same-rater descriptive associations do not identify a causal
concentration effect or a universal accuracy ceiling.

CAL-only predicted attributes still beat the training mean: Focus NDCG +0.2061
[0.1124, 0.3211]. External-only attributes give 0.4140. Combining them gives
0.4358, only +0.0012 over CAL-only; the full-versus-external-only gain is +0.0218
[-0.0331, 0.0797]. The attributes contain activity-related information, but this
bounded mapping does not add useful information beyond direct audio prediction.
Both native prediction loss and weak activity-label coverage remain plausible
limits; the evidence does not establish a single bottleneck.

Browser CAL500 attribute MAE ranges from 0.1154 to 0.1529, and MSE from 0.02072
to 0.03490. All ten values, both preparations and all fitted native heads are in
the report. External selected inner-CV MSE is recorded below as a **selection
diagnostic**, not an independent test of native transfer or Focus suitability:

| Source | Selected native MSE, in listed label order |
|---|---|
| DEAM arousal / valence | 0.01160 / 0.01018 |
| PMEmo arousal / valence | 0.01101 / 0.01290 |
| ManyMusic natural arousal / valence | 0.04239 / 0.04034 |
| ManyMusic generated arousal / valence | 0.01703 / 0.01305 |
| Memo arousal / valence | 0.00945 / 0.00715 |
| Soundtracks energy / tension / tender | 0.01625 / 0.02491 / 0.02730 |
| Emotify calmness / tension / power | 0.03264 / 0.02315 / 0.02339 |

## Coverage, calibration and captures

The common cohort has 405 mostly-vocal and 40 mostly-nonvocal songs, with five
intermediate cases. Their observed Focus fractions average 0.0920 and 0.2083.
Direct browser predictions average 0.0982 and 0.1886; attribute predictions
average 0.1013 and 0.1546. This supports inspecting vocal contrasts, not banning
vocals. High-energy and low-energy cells contain 223 and 79 songs, with mean
Focus fractions 0.0711 and 0.1399. Forty-six songs have Reading votes without
Studying votes; 71 have the reverse. All cell IDs, errors and score means are
retained. These descriptive cells are not separate confirmation populations.

The pooled Focus reference mean is 0.10210. Direct, attributes, hybrid and
shared browser scores average 0.10642, 0.10625, 0.10648 and 0.13471. Fixed
calibration bins and each activity are retained in the report. All candidate
Focus predictions lie below 0.4 in this cohort, so higher score bins have no
support. Shared normalized outputs have different semantics from native
listener fractions; this does not establish user-facing confidence thresholds.

The refreshed local snapshot has 84 songs, 18 Focus positives and 232 captures;
11 songs have fewer than three captures. Mean absolute first-to-all Focus-score
changes are 0.0241 direct, 0.0257 attributes and 0.0251 hybrid; maxima are 0.1096,
0.0922 and 0.1082. Three and all coincide because this snapshot has at most three
captures per song. Per-head and final first/three/all outputs replay, including
partial captures. There are no local Focus negatives or excerpt-level Focus
labels, so this cannot measure accuracy, distraction or temporal correctness.

## Exports and validation

| Candidate | Bytes | Maximum warm three-excerpt inference |
|---|---:|---:|
| Direct | 33,454 | 0.197 ms |
| Attributes | 431,503 | 1.780 ms |
| Hybrid | 464,354 | 1.901 ms |

All pass 1 MiB / 100 ms / 1 GiB bounds. Peak process RSS is 653,266,944 bytes;
local replay error is zero. Times cover Python scorer inference, excluding
encoding and application startup. Direct clips Reading/Studying per excerpt,
then averages; attribute models clip each native excerpt prediction before
averaging and mapping. The hybrid additionally uses a mean raw embedding.

Six focused checks pass, covering nested global exclusions, fixed family
weights, clipping before averaging, all saved outer native/final scores through
independent arithmetic, and independently recalculated ranking/top-list/MSE
values for every saved method and activity. Original #576 and new #615 receipts
verify. No global suite or test lock was used. Runtime: Python 3.14.8,
NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0; one numerical thread.

Frozen directory: `local/focus-615-20261030/round-1/`. Plan SHA256:
`5af390b1c1173e4d2978f6ca34835b955a4d81a6206c71965fb855f2d1545486`.
Report SHA256:
`8947e06c0c847230fb04ea379302307e72d51a2bad74f1ecf092d6278717d425`.
Direct export SHA256:
`39f6741b989bd1d665d4414506230d9b7b936285fbb52bf78b24af30e75da098`.
The evaluation receipt hashes all three exports and saved predictions; every
fit has its own model, complete selection/cross-fitting trace and receipt.

```text
./experiments/run.py focus-mapping verify
./experiments/run.py check --test test_focus_mapping.py
./experiments/run.py focus verify
```

To reproduce fitting, choose a new directory with `focus-mapping freeze
--directory ...` and then `focus-mapping run --directory ...`; preserve completed
attempts. Execution verifies frozen input, runtime and code hashes. Later code
changes require a new freeze. Source-term and identity limits remain in the
label audit. #615 and #576 have author evidence ready for independent review;
this report does not independently accept them or authorize productionization.
