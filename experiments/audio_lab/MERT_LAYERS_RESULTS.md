# MERT intermediate layers, #809

The fixed block-5/6 representation improves native MSE on 16/24 targets versus
the final layer, but **retain the existing specialists for all six vibes**.
None passes the prospective uncertainty and regression gates. No production
follow-up is justified by this development study.

All 288 recordings, 265 connected components and 576 exact ten-second intervals
match #708. The 1,536-dimensional representation concatenates temporal means of
hidden states 5 and 6. Final-layer vectors from the same passes match every old
vector exactly; three intermediate-vector replays also have zero error. Native
heads use the same nested grouped ridge selection and fixed mappings. All
nonpilot Memo recordings remain excluded.

## Fixed-mapping comparison

Mean differences across each vibe's predefined primary cases, middle minus
final layer. Larger NDCG/lift and smaller MSE are better. These are averages
across heterogeneous native references, not pooled human suitability labels.

| Vibe | NDCG difference | Top-list lift difference | Mapped MSE difference | Decision |
| --- | ---: | ---: | ---: | --- |
| Cruise | +0.00730 | -0.00618 | +0.00118 | Retain |
| Relax | -0.00809 | -0.01706 | -0.00121 | Retain |
| Uplift | +0.03147 | +0.02589 | -0.00459 | Retain |
| Energize | +0.03145 | +0.01916 | -0.00254 | Retain |
| Focus | +0.03259 | -0.00833 | -0.00403 | Retain |
| Release | +0.12814 | +0.07188 | -0.00392 | Retain |

Release's apparent gain comes entirely from Soundtracks source-only transfer:
NDCG +0.25628, paired component interval [-0.06822, +0.56992]. Its Jamendo
primary comparison has identical ranking/lift and worse MSE (+0.00309).
The interval does not establish a repeatable gain. Aggression prediction also
does not establish relief from anger. Uplift and Energize exceed mean gain
thresholds, but fail domain uncertainty/regression requirements. Focus fails
the lift and uncertainty requirements and uses Reading/Studying associations.

The machine-readable report includes every native MSE/MAE, mapped result,
1,000-draw paired component interval, source-only exclusion and failure reason.
Matched MusiCNN comparisons and exposed adopted-artifact diagnostics are also
retained; final-layer MERT is the primary representation control.

## Native MSE

| Native target | MusiCNN | Final MERT | Blocks 5/6 |
| --- | ---: | ---: | ---: |
| DEAM valence | .02898 | .03066 | .02510 |
| DEAM arousal | .01759 | .01914 | .01613 |
| PMEmo valence | .01011 | .01252 | .01270 |
| PMEmo arousal | .01342 | .01504 | .01188 |
| Soundtracks valence | .05157 | .05530 | .04198 |
| Soundtracks energy | .01704 | .03278 | .02695 |
| Soundtracks tension | .04247 | .03762 | .04114 |
| Soundtracks happy | .06318 | .07099 | .04224 |
| Soundtracks tender | .04958 | .06757 | .06204 |
| Soundtracks anger | .08049 | .04119 | .03586 |
| Jamendo happy | .16505 | .16209 | .17267 |
| Jamendo aggressive | .02720 | .03331 | .03550 |
| Emotify calmness | .03676 | .04182 | .04130 |
| Emotify tension | .02846 | .03476 | .02419 |
| Emotify joyful activation | .06036 | .06049 | .04764 |
| Emotify power | .03531 | .04123 | .04193 |
| ManyMusic natural valence | .04778 | .04088 | .03140 |
| ManyMusic natural arousal | .06798 | .04056 | .06623 |
| ManyMusic generated valence | .01469 | .01354 | .01433 |
| ManyMusic generated arousal | .02140 | .02373 | .01428 |
| Memo pilot valence | .00885 | .01180 | .00748 |
| Memo pilot arousal | .02004 | .02014 | .02244 |
| CAL500 Reading | .02122 | .02102 | .02086 |
| CAL500 Studying | .03823 | .04859 | .03487 |

## Resources and provenance

CPU-only Torch 2.6.0, Transformers 4.38.2, NumPy 1.26.4, Python 3.12.13;
one inference thread. Retained interval inference sums to 2,627.74 seconds
(mean 4.56, p95 5.74 seconds per ten-second interval). Resumed extraction wall
time is 2,736.74 seconds, including decoding and replay, and reuses 35.11 seconds
of completed inference from the interrupted attempt. Additional interruption,
loader investigation and failed validation overhead is not fully timed. These
figures are not an uninterrupted end-to-end runtime claim.

Peak worker RSS is 1,020.73 MiB; features plus chunk caches 41.56 MiB. Fitting
takes 27.18 seconds with peak RSS 534.71 MiB and 59,030,505 bytes of research
head packs. Evaluation takes 88.82 seconds; repeated head prediction for all
288 recordings takes .269–.286 seconds with zero error. Resource gates pass.
Browser feasibility and total preparation cost remain unmeasured here; #708's
prior encoder setup/extraction costs are preserved separately in the report.

Transformers prints a misleading weight-normalization key warning. A direct
audit verifies **all 211 tensors exactly match the pinned checkpoint**, including
both renamed tensors. The independent investigation reproduces this check.
This agrees with the upstream
[Transformers issue](https://github.com/huggingface/transformers/issues/26796).

Round 1 retains its original frozen extraction. A concurrent backward-compatible
receipt-verifier change prevented fitting under its source hash. Round 2 froze
the current code before fitting, references round 1's plan/receipt, and copies
only verified extraction outputs. No target comparisons were available before
the second freeze. Round 2 plan SHA256:
`99995bd895e7c613ed277dc35a6db82a8ef0a45196e59c69d9fb84b2c67d87ae`.

## Reproduction and checks

Default completed round: `local/mert-layers-809-20261101/round-2`.

```sh
./experiments/run.py representation-study verify
./experiments/run.py check --test test_representation_study.py
./experiments/run.py investigate representation_809.py
./experiments/run.py investigate mert_checkpoint_809.py
```

Results: 3 verified receipts; 2 focused checks, 2 actual-cohort checks and
1 checkpoint-tensor check pass. Source archives, input hashes, all vectors,
predictions, fitted heads and receipts remain inside the two local rounds.
To reproduce a fresh run, freeze a new directory before extract/fit/evaluate;
the frozen run.py and dependency sources preserve the exact historical code.
The completed dataset is development evidence; conditional intervals omit
training-set resampling, repeated study selection and unknown encoder overlap.
