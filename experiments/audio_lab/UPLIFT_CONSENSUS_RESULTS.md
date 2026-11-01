# Uplift consensus results, #618

30 October 2026. **Frozen research decision: retain.** Both declared consensus
procedures improve Soundtracks and Memo, but fail the predeclared per-domain
regression limits on ManyMusic. Neither passes that production-adoption rule. The
research is complete and ready for independent review; #580's earlier retain
result remains unchanged.

**Subsequent user decision:** after reviewing the comparison with production,
the user selected the fixed consensus and accepted the generated-music tradeoff.
Production integration is tracked separately in #630 and
[UPLIFT_PRODUCTIONIZATION.md](UPLIFT_PRODUCTIONIZATION.md). This decision does
not rewrite the frozen criteria, negative findings or uncertainty below.

The fixed blend gains 0.06274 mean NDCG over the refitted DEAM anchor and
0.04249 over the shared checkpoint. The learned blend gains 0.04547 and 0.02522.
Both pass the aggregate gain requirement and the positive paired-interval
requirement. **The reason for retain is the domain tradeoff**, which the frozen
protocol explicitly prohibits hiding with an average. The failing point
differences do not all have intervals excluding zero; this is a conservative
adoption rule, not a claim of conclusively established harm on every domain.

## Scope and reproducibility

The [protocol](UPLIFT_CONSENSUS_PLAN.md) was frozen before fitting. It compares
three procedures: DEAM valence alone, fixed native-source consensus, and a
learned nonnegative consensus selected inside nested training folds. Native
heads select constant/ridge models on their own human targets. Training uses
one mean embedding per annotated record; inference clips each excerpt's native
prediction before averaging. Source-weight selection uses that inference path.
No target source identity is an input to the scorer.

The [label audit](UPLIFT_LABEL_AUDIT.md) records counts, scales, rater support,
coverage and limitations. There are 3,998 development rows across eight cohorts:
1,800 DEAM, 766 PMEmo, 470 Soundtracks, 216 Jamendo human, 400 Emotify,
188 natural ManyMusic, 95 generated ManyMusic and 63 Memo. Joint recording,
artist, audio, declared-group and conservative equal-title components protect
both native fitting and source-weight selection. Five outer folds use three
nested folds; all native parameter selection remains inside training data.

The primary comparisons preserve the prior common cohorts: 470 Soundtracks,
171 natural ManyMusic, 95 generated ManyMusic and 63 Memo rows. Every displayed
method is scored on identical rows and fixed references within its table.
Joint folds differ from #580, so even unchanged frozen control predictions
produce different mean fold/top-list metrics. Do not compare this table's
absolute values directly with #580's 0.7318 Soundtracks or 0.8191 natural-music
figures. All populations were previously inspected; these are development and
transfer diagnostics, not fresh confirmation.

Frozen directory: `local/uplift-618-20261030/round-1/`.

| Artifact | SHA256 |
| --- | --- |
| `plan.json` | `2bf73a9f8f1360f50173f2cb3311dd1f31f9584a0d51054749704e5f28bb5b66` |
| `evaluation.receipt.json` | `2e05a7d63d4d1e4aab1e116aa5d5d52d84dd973a361ee913c7c042d40e25fb8b` |
| `models/full.json` | `a1f1087b75f844eef74be62b39d627df611be9c2edabdc99920cbb591342f737` |

The plan pins code archives, inputs, exact #580 controls and the new read-only
local snapshot. Every fit saves coefficients, nested selection traces, IDs,
predictions and a receipt. `report.json` contains every fold, calibration bin,
paired comparison, contrast ID, temporal diagnostic and decision failure;
`predictions.npz` and `semantic.npz` retain numerical outputs.

## Primary ranking and top-list results

NDCG uses linear graded relevance, averaged over five grouped folds. Each fold
selects the best 20% of rows, capped at ten, with score ties averaged. Higher
is better; scores are not percentages of correct classifications.

| Historical input | Shared checkpoint | Exact prior DEAM | Refitted anchor | Fixed blend | Learned blend |
| --- | ---: | ---: | ---: | ---: | ---: |
| Soundtracks | 0.64317 | 0.53676 | 0.56594 | 0.70807 | 0.68375 |
| ManyMusic natural | 0.70656 | 0.81864 | 0.81690 | 0.76473 | 0.77984 |
| ManyMusic generated | 0.91163 | 0.91750 | 0.90528 | 0.87718 | 0.89985 |
| Memo | 0.91971 | 0.88419 | 0.88419 | 0.97042 | 0.92403 |

The fixed blend beats the anchor in all five Soundtracks and all five Memo
folds, but loses in four of five natural and four of five generated ManyMusic
folds. Paired intervals use 1,000 whole-component bootstrap draws on the fixed
fits. They are conditional on these fitted models, without retraining or
accounting for previous inspection/model-search uncertainty.

| Fixed versus anchor | NDCG difference | Conditional 95% interval | Top-list lift difference |
| --- | ---: | --- | ---: |
| Soundtracks | +0.14213 | [+0.09480, +0.26168] | +0.08770 |
| ManyMusic natural | −0.05217 | [−0.11278, +0.01638] | −0.02815 |
| ManyMusic generated | −0.02810 | [−0.06116, +0.03890] | −0.00973 |
| Memo | +0.08623 | [+0.01207, +0.14022] | +0.05460 |

Against shared, fixed Soundtracks NDCG improves by 0.06490 with interval
[0.00018, 0.17808]; the lower bound barely clears zero. Its top-list lift
interval [−0.00085, 0.11478] includes zero. Fixed Memo's NDCG interval against
shared [−0.00001, 0.10698] also includes zero. The learned blend's natural
ManyMusic gain over shared is 0.07328 [0.00331, 0.14845], while its loss versus
the stronger anchor is 0.03706. These are different comparisons, both retained.

Historical top-list lifts above each fold's mean reference:

| Domain | Shared | Anchor | Fixed | Learned |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks | 0.11854 | 0.07548 | 0.16318 | 0.12473 |
| ManyMusic natural | 0.03755 | 0.12847 | 0.10032 | 0.10863 |
| ManyMusic generated | 0.10870 | 0.09449 | 0.08476 | 0.09963 |
| Memo | 0.09810 | 0.07797 | 0.13257 | 0.09933 |

The other exact controls remain in the numerical report:

| Domain | Refreshed calibration NDCG | Refreshed heads/calibration NDCG | Named PMEmo prior NDCG |
| --- | ---: | ---: | ---: |
| Soundtracks | 0.60511 | 0.65604 | 0.56624 |
| ManyMusic natural | 0.69444 | 0.67431 | 0.77128 |
| ManyMusic generated | 0.85550 | 0.87823 | 0.91241 |
| Memo | 0.87001 | 0.83905 | 0.91664 |

## Browser inputs and omitted-corpus transfer

Browser-prepared excerpts use the same models, with no refitting. The natural
ManyMusic failure persists: fixed loses 0.05928 NDCG and 0.03595 top-list lift
versus the anchor; learned loses 0.06067 and 0.04783.

| Browser outer NDCG | Shared | Prior DEAM | Anchor | Fixed | Learned |
| --- | ---: | ---: | ---: | ---: | ---: |
| Soundtracks | 0.64734 | 0.53841 | 0.56759 | 0.71462 | 0.69312 |
| ManyMusic natural | 0.70473 | 0.81834 | 0.82120 | 0.76192 | 0.76053 |
| ManyMusic generated | 0.91344 | 0.91477 | 0.90708 | 0.89136 | 0.89981 |
| Memo | 0.88963 | 0.87299 | 0.87228 | 0.97344 | 0.94875 |

Separate fits omit Soundtracks, both ManyMusic strata together, or Memo.
Every linked component is excluded from all remaining training corpora.
Omitting ManyMusic also removes three DEAM, 271 PMEmo, 29 Jamendo human and
19 Emotify rows. Soundtracks and Memo omission remove no additional rows in
other corpora. Fixed weights renormalize within the valence and emotion
families, preserving half the total per family.

| Historical omitted-corpus NDCG | Shared | Prior DEAM | Anchor | Fixed | Learned |
| --- | ---: | ---: | ---: | ---: | ---: |
| Soundtracks | 0.64317 | 0.53676 | 0.53676 | 0.65505 | 0.64526 |
| ManyMusic natural | 0.70656 | 0.81864 | 0.81868 | 0.75830 | 0.78827 |
| ManyMusic generated | 0.91163 | 0.91750 | 0.91750 | 0.88707 | 0.89255 |
| Memo | 0.91971 | 0.88419 | 0.88419 | 0.97344 | 0.92403 |

| Browser omitted-corpus NDCG | Shared | Prior DEAM | Anchor | Fixed | Learned |
| --- | ---: | ---: | ---: | ---: | ---: |
| Soundtracks | 0.64734 | 0.53841 | 0.55210 | 0.66327 | 0.65494 |
| ManyMusic natural | 0.70473 | 0.81834 | 0.81834 | 0.74861 | 0.79957 |
| ManyMusic generated | 0.91344 | 0.91477 | 0.91477 | 0.89753 | 0.90460 |
| Memo | 0.88963 | 0.87299 | 0.87299 | 0.97120 | 0.94011 |

Without Soundtracks training, both blends improve on DEAM, but their historical
gains over shared are only 0.01187 and 0.00209; both intervals include zero.
The larger development gain does not establish an equally large transfer gain.
Without ManyMusic training, both blends still fail: fixed natural loses 0.06038
NDCG versus the anchor; learned loses 0.03041. Generated top-list lift declines
versus shared by 0.01732 and 0.01439, beyond the 0.01 loss limit. The strict
nonregression rule applies to both historical and browser inputs and to the
anchor, exact previous DEAM and shared controls; all failures are listed in
`report.json`, without changing thresholds after seeing results.

## Native prediction and mapping diagnosis

Native held-out MSE is reported on each source's normalized target; these
numbers are not directly comparable across annotation constructs:

| Source | Native held-out MSE |
| --- | --- |
| DEAM | valence 0.010236 |
| PMEmo | valence 0.012769 |
| Soundtracks | valence 0.024873; happiness 0.036415 |
| Jamendo human | happy 0.114032 |
| Emotify | joyful activation 0.033905 |
| ManyMusic natural, matched 171 | valence 0.038264 |
| ManyMusic generated | valence 0.013630 |
| Memo | valence 0.006951 |

DEAM's transferred valence output has Soundtracks native-valence MSE 0.064140,
versus 0.024873 for a Soundtracks-native head. That supports a prediction/domain
mismatch before the Uplift mapping. Comparing the DEAM output directly with
Soundtracks happiness gives MSE 0.086870; this is a cross-construct diagnostic,
not a native happiness model evaluation.

For the fixed Soundtracks mean(happiness, valence) reference, the diagnostic
mapping from the other six **observed** attributes reaches NDCG 0.95547 and
MSE 0.007388. Using fully cross-fitted **audio-predicted** attributes reaches
0.75073 and 0.023755. There is useful information in the co-annotations, and
substantial loss in this audio prediction path. This diagnostic is not a fourth
adoption candidate: it has no full cross-corpus comparison or production claim.

The separate happiness-from-valence diagnostic reaches NDCG 0.65470 with
observed valence, and 0.49695 with predicted valence; MSE is 0.038640 versus
0.049298. Even observed valence does not recover happiness ranking well under
this declared mapping. The evidence supports both prediction error and an
incomplete semantic bridge; it does not isolate a unique causal explanation.

Calibration remains source-specific. On Memo, fixed improves ranking but
worsens mapped MSE from the anchor's 0.018163 to 0.027714; learned reaches
0.034852. On natural ManyMusic, corresponding MSEs are 0.040181, 0.045871 and
0.052102. Mixing lower-frequency happiness/joy outputs with VA scores changes
their absolute level. These are suitability estimates, not calibrated
probabilities of feeling better, and a lower raw score alone is not proof of
better or worse ranking.

## Musical contrasts, temporal coverage and local positives

At the frozen cutoffs, Soundtracks contains one gentle-happy excerpt, 32
energetic-negative excerpts and **zero positive-forceful excerpts**. This cannot
establish broad preservation of gentle-positive or positive-forceful music.
On the gentle excerpt, anchor/fixed/learned scores are 0.3270/0.3365/0.2951.
On energetic-negative excerpts their means are 0.4297/0.3502/0.3335, against
reference mean 0.1153. The blends reduce that overprediction without resolving
calibration. These are score diagnostics, not false-positive rates.

The low-arousal/positive-valence proxy has 15 DEAM, three PMEmo, 26 matched
natural ManyMusic, five generated ManyMusic and one Memo examples. For natural
ManyMusic the anchor/fixed/learned means are 0.5193/0.4837/0.4451, with reference
mean 0.7482. The fixed and learned mappings have no explicit energy gate, but
these results do not justify claiming gentle-positive coverage is solved.

Memo's aligned one/three/six-excerpt NDCG is 0.87625/0.91248/0.88419 for the
anchor, 0.97442/0.97104/0.97042 for fixed and 0.91473/0.93426/0.92403 for learned.
Each coverage uses its own matched human trajectory target. Whole-window
ManyMusic annotations cannot establish excerpt-level correctness.

The refreshed local set has 84 usable songs, 232 captures and 23 Uplift
positives. No identities or labels changed since #570. Mean first/all scores
on those positives are anchor 0.5509/0.5917, fixed 0.4250/0.4493 and learned
0.4120/0.4499. Unselected Uplift labels remain unknown. These previously
inspected positive-only checks provide no accuracy estimate or selection vote.

## Export, checks and disposition

The full learned procedure selects 50% DEAM, 25% Soundtracks and 25% Emotify;
the other experts receive zero. Four outer fits select this blend and one
selects 75% DEAM/25% Soundtracks. Omitting Soundtracks selects equal quarters
of DEAM, PMEmo, Jamendo and Emotify. Omitting ManyMusic selects 75% DEAM/25%
Soundtracks. These are training-selection results, not evidence that a
zero-weight corpus has no useful labels.

The complete research export contains all fixed and learned experts, including
zero-weight ones: **148,730 bytes**. Maximum warm three-excerpt inference was
**0.778 ms** (median 0.717 ms), peak process RSS **653,266,944 bytes**. Independent
full-export replay on all 232 local captures had maximum error zero. These
pass the frozen 512 KiB / 100 ms / 1 GiB resource limits.

Commands:

```sh
./experiments/run.py individual-local --directory experiments/audio_lab/local/uplift-618-20261030/local-1
./experiments/run.py uplift freeze
./experiments/run.py uplift run
./experiments/run.py uplift verify
./experiments/run.py check --test test_uplift_consensus.py --test test_uplift_replay.py
```

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0; one
numerical thread. Receipt verification passed; **8 focused checks passed** in
34.10 seconds. They cover nested
component exclusion, source balancing, blend constraints, ManyMusic paired
strata, clipping before averaging, finite saved outputs and independent
coefficient/blend replay of every held-out historical/browser prediction.
The initial synthetic clipping check lacked the fixture's required median
field; that fixture was corrected before freeze. No model fit failed, and
there was one frozen model attempt. Experiments did not acquire the global
suite lock; no full suite was run.

The completed follow-up belongs In review with a retain recommendation.
Soundtracks improvement is real evidence for broader emotion supervision,
but this bounded set of blends does not preserve the best valence behavior
across domains. A later experiment could test how to calibrate and combine
these native predictors while explicitly protecting gentle positivity. That
is a new hypothesis, not an already demonstrated fix. No production artifact,
production scoring path, commit or push is part of this investigation.
