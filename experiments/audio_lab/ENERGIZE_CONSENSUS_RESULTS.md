# Energize experiment results (#616)

30 October 2026. **Retain the current production Energize scorer.** The three
predeclared procedures do not meet the frozen improvement/nonregression rules.
The fixed consensus improves Soundtracks energy and some exercise rankings,
but loses on natural music and Memo. Training the mapping on exercise votes
does not solve those failures. Removing joy is a useful subsequent hypothesis,
not a broadly validated replacement selected by this round.

This completes the bounded author investigation. Independent acceptance remains
pending. No production code or model was changed. See the
[frozen plan](ENERGIZE_CONSENSUS_PLAN.md) and
[label and coverage audit](ENERGIZE_LABEL_AUDIT.md).

## What ran

- **Activation:** equal DEAM, PMEmo, ManyMusic and Memo source-family arousal
  predictions. Natural/generated ManyMusic split their family's weight equally.
- **Fixed consensus:** 50% activation, 25% Soundtracks energy, 12.5% Emotify
  power and 12.5% Emotify joyful activation.
- **Exercise mapping:** fixed-alpha ridge from those four separately represented
  inputs to original CAL500 Exercising votes, trained on cross-fitted native
  predictions. Direct audio-to-Exercising prediction is a control.
- **Five fixed component controls:** consensus without power or joy, and exercise
  mappings with activation alone, without power, or without joy.
- **Semantic diagnostic:** exercise prediction from seven observed co-annotated
  human attributes versus cross-fitted audio predictions of those attributes.
- **Transfer, browser and temporal checks:** five grouped outer fits, four
  omitted-corpus fits and one full research fit; separate historical/browser
  preparations; first/three/six aligned Memo chunks and first/three/all local
  captures. Ten main fit configurations in total.

There are 4,276 native-development source rows and 1,227 common primary
comparison rows. Known identity components are excluded across all sources
before native selection and exercise-map training. This includes conservative
title matches. All populations were previously inspected; these are development
results, not fresh confirmation. Missing identities and unknown encoder
pretraining overlap remain limits.

The unchanged shared artifact was checked against the current production model
before reusing its scores. Other installed specialists do not alter Energize.
Joint cross-source folds differ from #578, so even unchanged control scores have
different fold-specific NDCG/top-list summaries. Compare methods within the
tables below, not absolute scores between the two campaigns.

## Main comparisons

Historical-preparation grouped NDCG:

| Fixed reference | Rows | Production shared | Activation | Fixed consensus | Exercise mapping |
| --- | ---: | ---: | ---: | ---: | ---: |
| Soundtracks energy | 470 | 0.83838 | 0.81983 | **0.87240** | 0.86187 |
| ManyMusic natural arousal | 171 | **0.81900** | 0.79881 | 0.78118 | 0.78418 |
| ManyMusic generated arousal | 95 | **0.92398** | 0.90490 | 0.91923 | 0.91131 |
| Memo arousal | 63 | **0.93122** | 0.91613 | 0.91674 | 0.91674 |
| CAL500 Exercising | 428 | 0.28996 | 0.27570 | **0.30588** | 0.29746 |

Current browser-preparation control on the same rows:

| Fixed reference | Production shared | Activation | Fixed consensus | Exercise mapping |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks energy | 0.84344 | 0.82019 | **0.87594** | 0.85903 |
| ManyMusic natural arousal | **0.79242** | 0.79216 | 0.77513 | 0.76498 |
| ManyMusic generated arousal | 0.90020 | **0.90568** | 0.90373 | 0.90390 |
| Memo arousal | **0.93122** | 0.91199 | 0.90587 | 0.91089 |
| CAL500 Exercising | 0.25251 | 0.28759 | **0.33073** | 0.30710 |

The direct activity control reaches **0.37535 historical / 0.40246 browser** on
CAL500, exceeding the new activity mapping. It transfers poorly to Soundtracks
(0.74694/0.75610) and Memo (0.79680/0.80151). The exact previous PMEmo head also
remains stronger on generated music (0.94602/0.93893) and exercise
(0.37685/0.36933). Exact DEAM, Emotify, equal-VA, source-mean, prior selected and
refreshed shared controls remain in the machine report on supported rows.

These are ranking scores against different native references, not accuracy
percentages or interchangeable measures of product suitability.

## Top lists, uncertainty and the frozen decision

Fixed-consensus differences from production, with conditional paired 95%
intervals from 1,000 identity-group bootstrap draws:

| Reference/preparation | NDCG difference [interval] | Top-list lift difference [interval] |
| --- | --- | --- |
| Soundtracks historical | +0.03403 [−0.00187, +0.05473] | +0.01810 [−0.00610, +0.03720] |
| Natural historical | −0.03783 [−0.11169, +0.04974] | −0.02455 [−0.08267, +0.05314] |
| Generated historical | −0.00475 [−0.05219, +0.04278] | +0.00959 [−0.03061, +0.03525] |
| Memo historical | −0.01448 [−0.05532, +0.02495] | −0.00674 [−0.04162, +0.01945] |
| Exercise historical | +0.01591 [−0.07208, +0.13594] | −0.00333 [−0.05667, +0.07000] |
| Soundtracks browser | +0.03250 [+0.00064, +0.05626] | +0.01780 [−0.00813, +0.03746] |
| Exercise browser | +0.07822 [−0.05030, +0.14729] | +0.04333 [−0.03667, +0.08000] |

The browser Soundtracks ranking interval is positive, while its top-list
interval includes zero. No main candidate has a positive primary historical
NDCG interval over shared. The predeclared source-balanced historical macro
gains are **−0.016883 activation, +0.003544 consensus and −0.001807 exercise**,
all below +0.02. Natural-music ranking/top-list losses, browser Memo losses and
losses against prior PMEmo also exceed the frozen limits. Full per-domain
reasons are retained in `report.json`; no threshold was relaxed afterward.

## Transfer and component removals

Omitting Soundtracks entirely still gives consensus 0.87606 historical /
0.87936 browser, versus shared 0.83838/0.84344. Its energy benefit is therefore
not explained solely by including Soundtracks training labels.

Omitting both ManyMusic strata gives consensus **0.75249/0.76774 natural** and
**0.89459/0.90696 generated**. Omitting Memo gives **0.90777/0.91089**, below
shared 0.93122. The exercise mapping also regresses on omitted Memo
(0.88439/0.88436). Omitting CAL500 leaves no exercise-map supervision: activity
and direct-activity outputs are absent, not passed. Remaining activation and
consensus controls score 0.32544/0.27648 historically on that exercise reference.

The component controls reveal a tradeoff rather than a universally useful joy
term. Removing joy improves natural/generated/Memo browser ranking relative
to the complete consensus, while reducing Soundtracks and exercise ranking:

| Browser reference | Production shared | Complete consensus | Without joy | Without power |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks | 0.84344 | 0.87594 | 0.85145 | 0.88251 |
| Natural ManyMusic | 0.79242 | 0.77513 | 0.79176 | 0.77577 |
| Generated ManyMusic | 0.90020 | 0.90373 | 0.93017 | 0.87016 |
| Memo | 0.93122 | 0.90587 | 0.92397 | 0.88966 |
| Exercising | 0.25251 | 0.33073 | 0.27286 | 0.32448 |

The no-joy control also preserves browser omitted-corpus ranking reasonably
well: Soundtracks 0.84332, natural 0.79815, generated 0.92295, Memo 0.92717 and
exercise 0.29499. However, its historical natural score is 0.78843 versus
0.81900, and historical exercise is 0.25788 versus 0.28996. Memo top-list lift
falls by 0.01700 in both outer preparations. It was predeclared as a diagnostic
ablation, and these residual losses prevent claiming a broad improvement.

Exercise-map ablations do not resolve the problem: browser no-joy mapping
reaches 0.92558 on Memo but only 0.84157 Soundtracks and 0.29246 Exercising.
Activation-only exercise calibration preserves activation ranking; merely
changing the score scale does not create better rankings.

## What the human labels explain

On the common 428 CAL500 songs, exercise mappings from observed energy, beat,
tempo, danceability, power, cheerfulness and arousal achieve **0.46987 NDCG**.
Replacing those observations with cross-fitted audio predictions reduces the
score to **0.30014**; a training-mean control gives **0.15417**. Top-list lift
is 0.14671, 0.07585 and zero respectively.

The observed-versus-predicted ranking gap is +0.16974 with interval
[−0.00065, +0.31489]; its size remains uncertain. Observed attributes reduce MSE
by 0.001837, interval [0.000557, 0.003132]. Predicted attributes beat the mean
control in NDCG by +0.14597 [+0.03728, +0.26225]. There is useful mapping signal,
but the audio predictions lose some of it; these results do not identify a
unique cause or prove an attainable accuracy ceiling.

The label audit also finds sparse and inconsistent exercise votes: only
106/494 songs receive any vote, with median three raters and split-half exercise
correlation 0.116. Same-rater co-annotation, ordinal-encoding uncertainty and
whole-song labels paired with ten-second audio all limit the diagnostic.

Native held-out MAE is 0.0846 DEAM arousal, 0.0835 PMEmo arousal, 0.0966
Soundtracks energy, 0.1113 Emotify power, 0.1443 Emotify joy, 0.1663 natural
ManyMusic arousal, 0.1095 generated arousal, 0.0887 Memo arousal and 0.1065
CAL500 Exercising. These native tasks retain their own scales and support.

Calibration shows why ranking and raw error must stay separate. On historical
natural music, consensus MSE falls from shared 0.11940 to 0.04316 while ranking
gets worse. On Exercising, the exercise map reaches MSE 0.02269 versus shared
0.05832 and consensus 0.21657, while failing to beat the direct activity head's
ranking. Its mean score 0.08012 matches the exercise reference mean 0.07335,
but is far below arousal/energy scales on other corpora. None of these numbers
establishes cross-vibe calibration or a product strong-match threshold.

## Coverage and excerpts

At the frozen thresholds, Soundtracks has 23 energetic-cheerful and 32
energetic-unhappy examples, with no high-anger/low-energy examples. CAL500's
common cohort has 92 energetic-cheerful, 51 energetic/low-cheerfulness and 24
powerful/slower examples. Natural ManyMusic has 24 energetic-positive and five
energetic-negative examples; generated has eight and zero; Memo has zero in
both extreme valence/arousal slices. Empty cells remain unsupported, not wins.
All exact IDs, reference means and prediction means are in the report.

Memo's aligned first/three/six-chunk NDCG is:

| Procedure | First chunk | Three chunks | Six chunks |
| --- | ---: | ---: | ---: |
| Activation | 0.88672 | 0.90382 | 0.91613 |
| Consensus | 0.85759 | 0.89315 | 0.91674 |
| Exercise mapping | 0.87312 | 0.89558 | 0.91674 |

Each column uses its matching human trajectory interval, so this is not a
fixed-target causal comparison of excerpt counts. On 84 local songs, mean/max
absolute first-to-all score changes are 0.0497/0.2169 activation,
0.0514/0.2163 consensus and 0.0209/0.0961 exercise mapping. The 40 local Energize
positives remain supplementary; missing labels do not become negatives.

## Reproduction and verification

Frozen directory: `local/energize-616-20261030/round-1/`.

```sh
./experiments/run.py individual-local --directory experiments/audio_lab/local/energize-616-20261030/local-1
./experiments/run.py --log audio_lab/local/energize-616-20261030/freeze.log energize freeze
./experiments/run.py --log audio_lab/local/energize-616-20261030/run.log energize run
./experiments/run.py energize verify
./experiments/run.py check --test test_energize_consensus.py
```

Use new directories for a fresh freeze. Frozen inputs, native selections,
crossfit training identities, models, predictions, semantic traces and code
snapshots are retained. Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1 and
threadpoolctl 3.7.0; one numerical thread.

**Six focused checks passed in 43.99 seconds**, including independent replay of
all outer/transfer scores, independent primary NDCG/top-list/MSE recalculation,
component exclusion through both training stages, and exact frozen references.
The pre-fit check passed three synthetic checks and skipped the three checks
that require completed real results. Receipt/hash verification passed.

Supplemental read-only replay using the test's independent coefficient functions
checked 5,986 saved native outputs and all 252 first/three/all local cases
(2,268 method scores). Maximum absolute error was `1.5543122344752192e-15`.
The full research artifact is **150,152 bytes**; per-capture independent model
replay error is zero. Maximum warm three-excerpt inference is **0.918 ms**,
median 0.803 ms, peak process RSS 653,266,944 bytes. Resource checks pass;
timings exclude the encoder, extraction and application startup.

| Evidence | SHA256 |
| --- | --- |
| `plan.json` | `0500878ae3cc15b57cb840b3e5c4f0723029c026da7938ef53d0ae523cb35d42` |
| `report.json` | `b0063a608fd13ed94bfb88ecc5769e3b6d48c1260fe225ccbfec57a278d20f68` |
| `predictions.npz` | `6aa5ae4dcb485283956576919be516e1e56429b9026d13a802ce7de5c4f6605b` |
| `evaluation.receipt.json` | `b021c0f0373cdef06bc67e367fdad4b86403ecb15b9b733d1ef6e5999134578c` |
| `models/full.json` | `0801f0919f21f9ca7bcc1ad35d7ec872bc60db0c74227ab9423cfd3e9266660e` |

The live database was accessed read-only for the local export. No production
change, full suite, global-suite lock, commit or push occurred. Source terms
and existing consumer/output ownership remain unchanged. No additional
listening or shadow phase is required for this completed retain result.
