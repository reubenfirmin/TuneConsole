# Individual-vibe experiments #577–#580

30 October 2026. The bounded campaign is complete. **Retain the incumbent for
Release, Energize, Relax and Uplift.** Each has useful research results, but
none establishes a sufficiently reliable general replacement. The separate
[Focus experiment](FOCUS_EXPERIMENT_RESULTS.md) also recommends retaining the
current scorer. All changes and artifacts stay under `experiments/`.

Plan: `local/individual-20261030/remaining-1/plan.json`, SHA256
`c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`.
The [predeclared protocol](INDIVIDUAL_EXPERIMENT_PLAN.md) narrows the earlier
mapped-ranking search to native-MSE ridge selection within each representation.
It also strengthens identity protection. These are a comparison of complete
procedures, not an isolated causal estimate of changing the loss or model grid.

| Source | Native OOF rows | Protected transfer rows | Excluded directly | Extra connected exclusions |
| --- | ---: | ---: | ---: | ---: |
| Emotify | 400 | 255 | 145 | 0 |
| Jamendo explicit human votes | 216 | 187 | 29 | 0 |
| Soundtracks | 470 | 467 | 3 | 0 |
| Coimbra classes | 900 | 589 | 311 | 0 |
| EMMA matched previews | 333 | 236 | 97 | 0 |
| PMEmo | 766 | 347 | 262 | 157 |
| DEAM | 1,800 | 1,796 | 4 | 0 |
| CAL500 activities | 494 | 243 | 251 | 0 |

Native OOF retains the original five source-group folds; no current local
identity removes another native row. Transfer protects the union of local
identities, every other source, and the primary targets before its three-fold
source-only selection. PMEmo's 157 additional rows show why direct-match-only
exclusion is insufficient. Jamendo artist IDs are reconciled across the
`artist_...` and `artist:...` encodings, with explicit track-ID aliases too.

Missing artist names remain material: all 216 Jamendo and 470 Soundtracks rows,
1,751/1,800 DEAM rows and six Coimbra rows lack names. Jamendo artist IDs still
connect known Jamendo artists. Film groups and generated prompt groups control
validation folds without being treated as cross-corpus artist identities.
Unknown aliases and encoder pretraining overlap are unresolved.

| Primary cohort | Prior shared-disjoint rows | Common rows for this campaign |
| --- | ---: | ---: |
| ManyMusic natural | 186 | 171 |
| ManyMusic generated | 95 | 95 |
| Memo2496 pilot | 63 | 63 |
| Soundtracks | 470 | 470 |
| CAL500 activities | 450 | 428 |

The extra target exclusions protect the exact named prior full-source models
as well as the fixed/refreshed shared controls. All methods within a reported
task use its same retained rows. Do not compare this campaign's absolute
ManyMusic/CAL500 metrics directly with earlier tables on larger cohorts.

The source grids, target rules, inputs, legacy predictions, browser case
receipts, exclusions, source code and numerical runtime were pinned before
fitting. Existing broad-search and MERGE proxy evidence is retained by hash.
Pilot/browser inputs provide embeddings only; acoustic and combined comparisons
are restricted to native and historical Soundtracks inputs.

Eight protocol tests passed, including real 450-row independent Focus metric
recalculation, Jamendo aliases, connected collaborators, partial-capture
boundaries, fractional targets and unevaluable constant references. Three
independent results checks also passed: they verify archive/input/receipt
hashes, recompute every common-reference NDCG/top-mean/lift/MSE and paired
point difference, and replay the three principal sources' embedding
coefficients against actual historical/browser target rows without importing
the fitting, inference, mapping or ranking implementations.
The logs preserve normal bounded test-lock waits; no competing heavy session
or lock bypass was used.

Reproduction entrypoint:

```sh
./experiments/run.py individual freeze
./experiments/run.py individual fit
./experiments/run.py individual evaluate
./experiments/run.py individual verify
./experiments/run.py check --test test_individual_experiment.py --test test_individual_results.py
```

The existing frozen directory is preserved; a new freeze requires a new
`--directory`. The current local snapshot has 84 previously inspected
identities. Only existing positive labels and captures enter supplementary
diagnostics; unselected vibes remain unknown.

## Release #577: retain; keep the new Jamendo head as a research candidate

All figures below use the same 470 Soundtracks anger references. Power,
aggression and anger remain distinct native tasks; no VA label supplies
Release truth. Old source-selected controls and fixed/refreshed shared
artifacts are retained exactly.

| Method | NDCG | Top-list lift | MSE |
| --- | ---: | ---: | ---: |
| Frozen shared | 0.6217 | 0.2928 | 0.06476 |
| Prior Emotify selected | 0.6502 | 0.2858 | 0.03847 |
| Prior Jamendo selected | 0.6452 | 0.2832 | 0.03967 |
| New Emotify embedding | 0.4587 | 0.1639 | 0.04004 |
| New Emotify combined | 0.6053 | 0.2698 | 0.03915 |
| New Jamendo embedding | 0.7111 | 0.3447 | 0.04118 |
| New Jamendo combined | 0.7043 | 0.3395 | 0.03973 |
| New Jamendo, browser inputs | 0.7112 | 0.3452 | 0.04410 |
| Fixed equal Emotify/Jamendo embedding mean | 0.6741 | 0.3231 | 0.03778 |

Refreshed-calibration/refreshed-both NDCG is 0.6688/0.6619; browser shared is
0.6086. New Jamendo's ranking difference from the checkpoint is +0.0894,
paired interval **[-0.0082, +0.1911]**. Top-list lift differs by +0.0519
[-0.0111, +0.1285]. Browser preparation does not remove that uncertainty.
The equal ensemble's NDCG difference is +0.0524 [-0.0065, +0.1411]. Emotify
embedding regresses by -0.1629 [-0.2405, -0.0141]; favorable native performance
does not justify including it in a target model.

Native Jamendo aggression OOF improves from prior MAE-head NDCG 0.5971 to
0.6736 with embedding ridge, MSE 0.04398 versus 0.05151. Native Soundtracks
anger reaches 0.7909 embedding / 0.8068 combined versus training mean 0.2191.
These are source OOF results, not unseen predictions from a full Soundtracks
fit. Reciprocal embedding transfers are Emotify to Jamendo 0.5947 (random
0.2221), Jamendo to Emotify 0.7024 (0.3175), Soundtracks to Emotify 0.5805
(0.3175), and Soundtracks to Jamendo 0.7085 (0.2221). Their constructs and
scales remain separate.

New Jamendo's mean score is 0.0820 against mean human anger 0.1534, so its
ranking does not establish interchangeable aggression/anger calibration.
Coimbra and EMMA remain weaker supplementary controls; all feature variants
are retained in `primary.report.json`. Publisher terms and commit are pinned
in the plan. The new Jamendo exports remain research-only and inherit no
earlier artifact's terms acceptance.

## Energize #578: retain; generated-music gains do not transfer consistently

The table gives NDCG for fixed embedding procedures within each reference.
Soundtracks uses human energy; CAL500 uses original Exercising vote fractions.

| Reference | Frozen shared | PMEmo | DEAM | Equal VA | Emotify |
| --- | ---: | ---: | ---: | ---: | ---: |
| ManyMusic natural | 0.7979 | 0.7646 | 0.6903 | 0.7661 | 0.7140 |
| ManyMusic generated | 0.9187 | 0.9647 | 0.9162 | 0.9442 | 0.9104 |
| Memo2496 | 0.9160 | 0.8992 | 0.8271 | 0.8804 | 0.8634 |
| Soundtracks energy | 0.8489 | 0.8184 | 0.8537 | 0.8320 | 0.8780 |
| CAL500 Exercising | 0.2948 | 0.3966 | 0.2170 | 0.2914 | 0.1936 |

Equal VA's natural-music difference is -0.0318 [-0.0966, +0.0130], and Memo's
is -0.0356 [-0.0855, +0.0163]. Top-list lift also declines on those domains.
PMEmo's generated-music improvement is +0.0460 [-0.0012, +0.1033]; it cannot
justify replacing the better natural/Memo scorer. The named prior acoustic
PMEmo head scores 0.5992 on Soundtracks; the new acoustic procedure reaches
0.7566, still below 0.8489. Combined PMEmo is 0.8011. Emotify combined's
0.8810 on Soundtracks does not erase its pilot failures.

Reciprocal arousal transfers are 0.7445 PMEmo to DEAM (random 0.5938), and
0.8918 DEAM to PMEmo (0.6967). The new native CAL500 embedding head scores
0.4703 on all 494 source OOF rows, versus prior MAE 0.3533 and training mean
0.1531. On the common 428-row slice it scores 0.3869. Direct activity labels
provide useful signal without establishing a general replacement. All
refreshed/browser controls, source means, errors and fold failures remain in
the report; no ensemble weights were retuned.

## Relax #579: retain; Emotify is promising, but pilot gains remain uncertain

| Reference | Frozen shared | PMEmo | DEAM | Equal VA | Emotify embedding |
| --- | ---: | ---: | ---: | ---: | ---: |
| ManyMusic natural | 0.7610 | 0.7904 | 0.6823 | 0.7791 | 0.7850 |
| ManyMusic generated | 0.8607 | 0.9009 | 0.8434 | 0.8701 | 0.8722 |
| Memo2496 | 0.8469 | 0.8619 | 0.7661 | 0.8647 | 0.8936 |
| Soundtracks low energy/tension plus tenderness | 0.5799 | 0.4879 | 0.4318 | 0.4758 | 0.7958 |
| CAL500 sleep proxy | 0.5620 | 0.3409 | 0.2504 | 0.4375 | 0.5818 |

Emotify embedding's pilot NDCG differences are +0.0240 [-0.0472, +0.0960],
+0.0115 [-0.0273, +0.0898], and +0.0466 [-0.0158, +0.0864]. Pilot top-list
intervals also include no improvement. The Soundtracks gain is clearer:
+0.2159 [+0.1174, +0.3013], top-list lift +0.1540 [+0.0767, +0.2257]. Browser
inputs preserve the pattern, but the named prior Emotify combined ridge is
still higher there at 0.8170; new combined is 0.8036. The exact combined prior
cannot be evaluated on pilot caches lacking its historical acoustic features.
That unsupported comparison is not counted as a pass.

Equal VA fails Soundtracks: top-list lift is -0.07445, below random, and its
NDCG difference from the checkpoint is -0.1040 [-0.2200, -0.0002]. Both
reciprocal VA embedding Relax transfers also fall below target random
rankings: PMEmo to DEAM 0.7236 versus 0.7263; DEAM to PMEmo 0.7328 versus
0.7659. Averaging low-arousal/positive-valence sources does not reliably
substitute for calmness/tension supervision.

Native Emotify embedding OOF is 0.8397 versus prior MAE 0.8370 and training
mean 0.6508, with MSE 0.01959 versus 0.01938/0.03104. Preserve that native
signal and the promising transfer candidate without claiming a confirmed
product gain. Jamendo relaxed votes and CAL500 sleep remain different
references; earlier MERGE proxy evidence is retained without another fit.

## Uplift #580: retain; positive-valence transfer has a material domain failure

| Reference | Frozen shared | Named prior PMEmo | PMEmo | DEAM | Equal VA |
| --- | ---: | ---: | ---: | ---: | ---: |
| ManyMusic natural | 0.7084 | 0.7782 | 0.7497 | 0.8191 | 0.7855 |
| ManyMusic generated | 0.9219 | 0.9203 | 0.8975 | 0.8944 | 0.9240 |
| Memo2496 | 0.9162 | 0.9055 | 0.9045 | 0.9522 | 0.9180 |
| Soundtracks happiness/valence | 0.7318 | 0.6395 | 0.6509 | 0.5533 | 0.6414 |

DEAM's natural-music NDCG gain is +0.1108 [+0.0316, +0.1610], top-list lift
+0.0952 [+0.0143, +0.1413]. On Soundtracks it regresses by **-0.1785
[-0.2683, -0.0361]**; its generated-music point estimate also declines. The
equal ensemble does not fix the domain problem, and its Memo top-list lift
is slightly lower than the checkpoint despite slightly higher NDCG.
Emotify joyful activation is 0.7698/0.8880/0.9194/0.6580 across these four
references, so it is not a consistent alternative either.

Native PMEmo embedding OOF is 0.8978, identical to the prior MAE procedure;
DEAM is 0.8934 versus prior 0.8898. Reciprocal valence transfers remain useful
at 0.7699 PMEmo to DEAM and 0.8874 DEAM to PMEmo, but do not predict success
on every emotion reference. None establishes a causal improvement in a
listener's mood.

## Calibration, distinctions and changing excerpts

Reports retain separate native-column MSE/MAE, mapped MSE, five fixed
calibration bins, top-set means/lift, every fold and all paired contrasts
against fixed/refreshed/browser and source-mean controls. Large MSE reductions
against the shared model partly reflect its normalized competing scores:
natural-music Relax reference mean is 0.5431, Emotify score mean 0.5259 and
shared mean 0.0676. That does not make native scores calibrated probabilities
of user benefit. On the sleep proxy the Emotify error instead rises
substantially. No target-fitted calibration removes these scale differences.

Intervals use 1,000 whole-group draws within fixed folds, condition on fixed
fits and previously inspected corpora, and have no multiple-search correction.
No candidate is promoted by selecting a favorable domain or interval.

Fixed Soundtracks slices contain 47 high-energy/low-anger excerpts, 32
energetic negative-valence excerpts, five low-energy/tense excerpts and 23
low-energy/sad excerpts. New Jamendo Release averages 0.0975 in the first
slice versus 0.2073 on five high-anger excerpts with estimated tempo at most
90 BPM. Their human anger mean is 0.6973: force need not require a fast
estimated tempo, but the transfer scores understate this anger scale. Tempo
is an unvalidated acoustic estimate, not a human tempo annotation.

There are zero high-anger/low-energy rows at the frozen thresholds and only
one gentle-happy row. Those distinctions remain unestablished. A supplementary
descriptive intersection of the same thresholds, computed after evaluation,
contains 23 high-energy/happy/low-anger excerpts: Jamendo Release averages
0.0954, PMEmo Energize 0.5022 and DEAM Uplift 0.5038. This post-evaluation
slice changes no fit, threshold, selection or acceptance decision. Human
quietness and distraction labels remain unavailable. Sad low-energy music
can score highly under the declared Relax mapping; this does not make it
positive-valence Uplift.

Memo's trajectories support aligned first/three/all-chunk references. PMEmo
Energize's mean absolute first-to-all score change is 0.0433, maximum 0.2109;
aligned MSE is 0.01738/0.01721/0.01422. Uplift changes by mean 0.0534, maximum
0.3105, with aligned MSE 0.03704/0.03131/0.03040. Emotify Relax changes by
mean 0.0312, with aligned MSE 0.01494/0.01546/0.01424. These windows have
different references; they are capture diagnostics, not proof that every
additional excerpt improves a user's result. ManyMusic supplies whole-window
truth only, so per-chunk correctness there is unknown.

The frozen local export has 7 Release, 40 Energize, 15 Relax and 23 Uplift
explicit positives among 84 recordings, with 11 partial captures and zero
new identities. `local.report.json` preserves per-source first/three/all scores
and checkpoint outputs. It supplies neither false-positive rates nor a new
independent cohort.

## Artifacts, verification and disposition

All 24 protected source/representation models replay every native head with
zero discrepancy. The 81 slim single-vibe exports and four declared ensemble
exports also replay independently. All 85 pass the frozen resource bounds;
the largest is 66,746 bytes and the slowest of the measured 100 warm
three-excerpt calls is 0.326 ms. Reported process peak RSS is 653,266,944 bytes.
These Python measurements include process/runtime overhead and exclude the
encoder, browser/application startup and production consumers.

Eight protocol checks and three independent real-artifact checks passed.
Syntax checks covered 137 research Python files. Completed verification checks
the plan, every frozen input/archive/reference, eight fit receipts and the
evaluation receipt. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1,
threadpoolctl 3.7.0. Evaluation receipt SHA256:
`156b9689e2a757f960b129c83f69c18b1aa30db7ef16bcd6cc08fa007cebb863`.

Each of #577–#580 has a **retain/no-go recommendation for this frozen
experiment**. Research outputs are complete and reviewable; this does not
move tickets to Done or establish production acceptance. #573/#587 still own
their production/output checks, #405 retains its #586/#587 dependency, and
#137's genre calibration remains separate. No production code, model
configuration, live database or repository commit was changed by this work.
