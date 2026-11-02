# Chroma complement, #810

Follow-up #815 now recommends **retaining the incumbent**: the larger 494-track
development study against its full protected recipe fails the frozen improvement
checks. See [Focus confirmation results](FOCUS_CONFIRMATION_RESULTS.md). The
original #810 finding below records why that follow-up was warranted; it does
not establish a current deployment recommendation.

**Advance concatenated MusiCNN/chroma for a separate Focus confirmation and
deployment study. Retain the other five specialists.** Both chroma variants
pass the predefined Focus development gates; chroma-only remains the planned
diagnostic. This is not permission to replace the shipped Focus model: the
primary comparator is a matched nested MusiCNN control on 32 CAL500 recordings,
not an independent evaluation of the exact shipped artifact.

The frozen study contains the same 288 recordings, 265 connected components,
576 ten-second intervals and source-family exclusions as #708. Every 16 kHz
PCM hash matches, original embedding/control predictions remain unchanged,
and three chroma replays have zero error. CQT summaries have 72 dimensions;
their concatenation with the embedding has 272. The private dependency lock,
prospective sources and all inputs/predictions/heads are retained under
`local/chroma-810-20261101/round-1`. Plan SHA256:
`818edf0d5346ca20e2bfeb2be6aa16c4540e00c659ae17f70706686c906876a2`.

## Fixed-mapping comparison

Mean primary differences versus matched MusiCNN; larger ranking/lift and
smaller MSE are better. The report contains every domain and 1,000-draw paired
component interval. Native scales remain separate.

| Vibe | Chroma NDCG | Chroma lift | Combined NDCG | Combined lift | Combined MSE | Combined decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Cruise | +.02455 | +.01852 | +.01310 | +.00622 | +.00090 | Retain |
| Relax | -.06297 | -.04784 | -.01650 | -.01846 | +.00062 | Retain |
| Uplift | -.03471 | -.02325 | +.01809 | +.01378 | -.00156 | Retain |
| Energize | -.08194 | -.07304 | +.00257 | +.00444 | -.00024 | Retain |
| Focus | +.59466 | +.18750 | +.58896 | +.17917 | -.00666 | Candidate for review |
| Release | -.21474 | -.04164 | -.13326 | -.06098 | +.00182 | Retain |

Chroma-only improves native MSE on 7/24 targets, combined on 12/24. Neither
offers a general emotion-model replacement. Cruise chroma-only clears mean
gain thresholds but fails uncertainty/domain regression requirements. The
other non-Focus decisions also fail their fixed gates.

## Focus evidence and limits

CAL500 has 32 recordings and 31 connected components, split 10/9/6/3/4 across
the inherited outer folds. References average human Reading/Studying votes.

| Metric | MusiCNN | Chroma | Combined |
| --- | ---: | ---: | ---: |
| Mean fold Focus NDCG | .22167 | .81633 | .81063 |
| Mean fold top-list lift | -.04787 | .13963 | .13130 |
| Mean fold mapped MSE | .01795 | .01395 | .01129 |
| Reading native MSE (recording average) | .02122 | .01538 | .00975 |
| Studying native MSE (recording average) | .03823 | .03095 | .03422 |

Combined-minus-control component intervals: NDCG [.02828,.69224], top-list
lift [.00406,.22500], mapped MSE [-.01156,-.00244]. Four of five combined folds
rank above random; the three-recording fold regresses. Studying alone has an
uncertain ranking gain. The exact exposed adopted Focus control has NDCG
.56821 and MSE .01406 on these rows, but its training exposure prevents a clean
generalization comparison. Do not advertise the .589 improvement as a measured
gain over production.

After seeing this result, a separately documented diagnostic fixed 99 paired-
label permutations and refitted both models through the entire nested process.
The 30 singleton components' paired targets were permuted; the repeated
two-recording component was held fixed. Null NDCG-difference quantiles are
[-.28856,0,.29814]; one draw meets/exceeds the observed .58896, giving corrected
tail fraction .02. This 41.30-second diagnostic is conditional, post-result and
not multiple-study-adjusted. It reinforces a follow-up hypothesis, not an
independent confirmation. See `CHROMA_FOCUS_DIAGNOSTIC.md` and
`local/chroma-810-20261101/focus-permutation/`.

Future work must use genuinely held-out activity references where available,
compare the exact incumbent or a leakage-safe retraining of its recipe, and
validate browser DSP, startup, runtime, memory, persistence and old-client
compatibility before any production change. Activity association does not
establish cognitive performance. No additional user listening is required.

## Resources and checks

Private Python 3.12.13 / Librosa .11.0 / NumPy 1.26.4 / SciPy 1.14.1,
single numerical thread. Extraction 155.94 seconds, including decoding;
first computation 18.28 seconds with compilation, warm median .05335 seconds,
p95 .06641 seconds per ten-second interval. Worker RSS 328.18 MiB; feature NPZ
1,435,696 bytes. Private environment logical file size 482,318,177 bytes.
Setup downloaded roughly 110 MiB of package payload within the fixed budget.
Fit 6.68 seconds, peak RSS 534.71 MiB; research head packs 13,590,308 bytes.
Evaluation 100.34 seconds. Combined-head replay for all 288 recordings takes
.028–.029 seconds with zero error. Browser cost remains unmeasured.

```sh
./experiments/run.py chroma-study verify
./experiments/run.py investigate chroma_810.py
./experiments/run.py investigate chroma_focus_810.py
```

Three main receipts verify; all three actual-cohort/pitch/silence checks pass.
The supplemental diagnostic check passes separately. Its completed report is
preserved; it intentionally refuses to overwrite itself. To rerun that
diagnostic, choose a new output directory. For fresh extraction/model fitting,
freeze a new directory and run `chroma-study extract`, `fit`, `evaluate` with
that directory. Original source snapshots retain the exact executed version.
No production code or database changed; all nonpilot Memo data remain excluded.
