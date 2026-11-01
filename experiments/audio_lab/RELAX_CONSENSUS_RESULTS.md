# Relax consensus results, #617

**Recommend the fixed blend for review and possible productionization. Retain
the existing scorer rather than adopting the learned-weight blend.** The fixed
procedure passes the frozen research thresholds; this is author evidence on
previously inspected corpora, not independent acceptance or fresh confirmation.
No production scorer or application database was changed.

The fixed blend is 50% Emotify calmness/inverse tension, 25% Soundtracks
energy/tension/tenderness, 12.5% ManyMusic, and 12.5% Memo. ManyMusic's share is
split equally between its natural/generated native VA experts. Native heads are
selected on grouped training-fold error. Blend weights were fixed before fitting.
Positive valence is supporting evidence in the VA experts, not a prerequisite
for Relax. The anchor always contributes at least half the score.

## Scope and refreshed labels

#579 owns the original single-source experiment; #617 owns this user-authorized
consensus follow-up. The protocol is `RELAX_CONSENSUS_PLAN.md`. It compares three
procedures: a refitted Emotify anchor, the fixed blend, and a constrained learned
blend. This replaces the earlier proposed activation/tenderness-only search.

The live database was read through read-only SQL without opening a Store or
running migrations. The refreshed snapshot has **84 usable labeled songs,
including 15 Relax positives**, plus three labeled songs without usable captures.
This includes the growth from the original 55-song diagnostic; it has no new
usable identities beyond the 84 already frozen for #570. Unselected vibes remain
unknown, and local annotations neither fit nor select this model.

| Source | Training rows | Groups within source | Primary evaluation rows |
| --- | ---: | ---: | ---: |
| Emotify | 400 | 138 | 400 native OOF |
| Soundtracks | 470 | 295 | 470 |
| ManyMusic natural | 188 | 166 | 171 |
| ManyMusic generated | 95 | 88 | 95 |
| Memo | 63 | 62 | 63 |

These are corpus rows, not a claim of independent identities across corpora.
Global components join known recording, audio, artist, declared-group and
conservative equal-title links before splitting. Entire components are withheld
at every outer/inner stage and removed from training when a corpus is omitted.
The new local snapshot excludes no further external rows. The old comparator
exposure mask leaves 171 natural ManyMusic rows for matched evaluation.

ManyMusic's supplied inventory contains 1,695 labeled windows and 283 audio files;
this experiment retains the original identity-selected window per recording.
Memo has 64 acquired files, of which 63 have complete labels for the declared
interval. Neither extra windows nor additional raters become independent songs.
The full published Memo collection has not been acquired for this experiment.

## Matched ranking results

NDCG below uses **the new joint grouped folds for every method**. The same old
predictions therefore produce different summary NDCGs from #579's earlier fold
tables. No old predictions or labels were changed. All comparisons within a row
use identical references and recordings. ManyMusic/Memo VA remains a proxy for
Relax; raw scores across different references are not interchangeable.

| Reference | Shared scorer | Previous Emotify embedding | Refitted anchor | Fixed blend | Learned blend |
| --- | ---: | ---: | ---: | ---: | ---: |
| Soundtracks | 0.5524 | 0.7782 | 0.7944 | **0.8409** | 0.8518 |
| ManyMusic natural | 0.7568 | 0.7890 | 0.7838 | **0.8111** | 0.7863 |
| ManyMusic generated | 0.8403 | 0.8229 | 0.8389 | **0.8687** | 0.8709 |
| Memo | 0.8104 | 0.8932 | 0.9143 | **0.9258** | 0.9258 |

Equal-corpus mean NDCG improvement for the fixed blend is **+0.04310** over the
previous embedding model and **+0.02887** over the refitted anchor. Natural and
generated ManyMusic each receive half of one corpus's weight in this summary.

| Reference | Fixed gain over previous embedding | Conditional paired 95% interval | Top-list lift gain |
| --- | ---: | --- | ---: |
| Soundtracks | +0.06273 | [+0.01546, +0.10271] | +0.01908 |
| ManyMusic natural | +0.02216 | [-0.03246, +0.04503] | +0.01650 |
| ManyMusic generated | +0.04576 | [-0.01618, +0.07536] | +0.03582 |
| Memo | +0.03261 | [+0.00082, +0.04987] | +0.02272 |

There are 1,000 paired component-bootstrap draws. These intervals condition on
the fitted predictions; they do not include a new corpus draw or all model-
selection uncertainty. The Memo interval barely excludes zero and its pilot is
small. Top-list lift intervals against the previous embedding control include
zero on all four sets. Against the refitted anchor, Soundtracks has clearer
ranking and top-list evidence: +0.04655 NDCG [0.01541, 0.09373] and +0.02595 lift
[0.00658, 0.07301]. The fixed blend improves Soundtracks ranking in all five
outer folds, natural/generated ManyMusic in four, and Memo in four with one tie.

The older combined Emotify control reaches **0.81255** on these Soundtracks folds,
below the fixed blend's 0.84094. Its historical acoustic inputs are unavailable
in the ManyMusic/Memo caches (`audio_attributes=null`). The missing comparisons
remain explicit; different modern features were not substituted into that model.

Browser-prepared NDCGs preserve the fixed-blend advantage:

| Reference | Previous embedding | Fixed blend |
| --- | ---: | ---: |
| Soundtracks | 0.7813 | 0.8388 |
| ManyMusic natural | 0.7881 | 0.8015 |
| ManyMusic generated | 0.8503 | 0.8852 |
| Memo | 0.8836 | 0.9243 |

Both refreshed shared controls, calibration tables, errors and all per-fold
results remain in `report.json`; the tables above are selected columns, not
separate evaluations. These scores are not calibrated probabilities of benefit.

### Subsequent aggregation check

The user asked why Relax averages embeddings before scoring. That was an explicit
pre-fit design choice: one feature vector per annotated interval, with training
and inference using the same aggregation. It was not selected by comparing
aggregation methods and does not establish a Relax-specific advantage.

A subsequent read-only check reused the exact five outer models, original chunk
embeddings, matched rows and fixed weights, changing only inference order to
average the separately clipped chunk predictions. These ridge heads and fixed
maps are linear before clipping; clipping creates the observed differences.
NDCG was unchanged on Soundtracks, generated ManyMusic and Memo, on both
historical and browser inputs. Natural ManyMusic changed from 0.811145924 to
0.810887061 historical, and 0.801468172 to 0.803213627 browser. Top-list lift
was unchanged in all eight comparisons. The largest per-recording score change
was 0.011667802, on historical generated ManyMusic.

This post-fit diagnostic does not change the frozen plan, models, predictions or
original decisions, and is not a comparison of separately retrained procedures.
It finds no consistent advantage for either inference order. A production choice
to match Cruise/Release's per-excerpt prediction aggregation is therefore
reasonable, provided its exported runtime is replayed against that explicitly
chosen order. Mean-embedding aggregation is not intrinsically required by Relax.

The user subsequently authorized that serving order and production integration
of the fixed blend under #629. [Production evidence](RELAX_PRODUCTIONIZATION.md)
records exact coefficient export, real-capture replay and packaged inference;
the original research artifacts and conclusions remain frozen.

## Withholding an entire corpus

For each row below, the target corpus is absent from fitting and weight selection.
Both ManyMusic strata leave together. Removing ManyMusic also removes 19 linked
Emotify rows, leaving 381. Fixed auxiliary weights are renormalized among the
remaining corpora while Emotify retains half the total weight.

| Omitted reference | Previous embedding | Fixed blend | Learned blend |
| --- | ---: | ---: | ---: |
| Soundtracks | 0.7782 | 0.8057 | 0.8152 |
| ManyMusic natural | 0.7890 | 0.7839 | 0.7679 |
| ManyMusic generated | 0.8229 | 0.8523 | 0.8650 |
| Memo | 0.8932 | 0.9179 | 0.9179 |

The fixed blend's small natural-ManyMusic loss is -0.00509 historical and
-0.01157 browser NDCG, within the predeclared 0.02 limit. Every historical/browser
transfer comparison stays within the 0.02 NDCG and 0.01 top-list-lift regression
limits against both anchor controls. Most transfer intervals include zero:
passing these point-estimate limits does not prove noninferiority.

The learned blend fails the same rule. Without ManyMusic, natural-ManyMusic
NDCG falls by **0.02108** and lift by **0.02246** against the previous embedding
control. Without Soundtracks, browser Soundtracks lift falls by **0.01844**.
These losses have uncertain paired intervals, but exceed the frozen limits.
Its full-data selected weights are 50% Emotify, 37.5% Soundtracks, 12.5% ManyMusic,
and 0% Memo. Selection varies across folds; it does not establish that Memo is
generally useless. Further weight searching was not performed after these results.

## Native labels, contrasts and local captures

Independent replay of the outer native heads gives these per-label MSEs:

| Source | Native OOF MSE |
| --- | --- |
| Emotify | calmness 0.031695; tension 0.022911 |
| Soundtracks | energy 0.015635; tension 0.025926; tenderness 0.028690 |
| ManyMusic natural | valence 0.041298; arousal 0.044527 |
| ManyMusic generated | valence 0.014508; arousal 0.017570 |
| Memo | valence 0.007600; arousal 0.012041 |

Observed-attribute diagnostics exclude every reference-defining column. On
Emotify, the remaining observed emotions predict the fixed calmness/tension
reference at 0.87949 NDCG, falling to 0.81568 when those attributes must themselves
be predicted from audio. On Soundtracks the corresponding figures are 0.87967
and 0.85025. These diagnostics support available semantic information but also
show prediction limits; they do not identify which added label causes improvement.
There is no co-annotated calmness/tension target in the VA-only datasets.

At the frozen cutoffs, Emotify has eight quiet/tense cases: the fixed blend lowers
their mean score from 0.4584 to 0.4206. Soundtracks has five such cases, lowered
from 0.5337 to 0.4974. Both remain above their reference means, so tension detection
is imperfect. The 21 low-tension/sad Soundtracks excerpts still score higher at
0.6158, though below the anchor's 0.6522. There are no Emotify rows jointly above
0.65 calmness and sadness. Low-arousal/negative-valence proxy counts are 12
natural ManyMusic, seven generated, and one Memo; these are not direct calm/sad
annotations. This coverage cannot establish all desired subjective distinctions.

The current 15 explicit local Relax positives have complete-capture mean scores
of 0.5045 anchor, 0.4576 fixed, and 0.4346 learned. First-capture means are
0.5232/0.4724/0.4518. This is a score-scale/stability diagnostic, not an accuracy
comparison: the remaining songs are unlabeled for Relax, not negatives. Records
and first/complete scores for all 84 real captures are retained in the report.

## Verification and retained artifacts

Run directory: `local/relax-617-20261030/round-1/`; refreshed captures:
`local/relax-617-20261030/local-1/`. The source snapshot, all input identities,
five outer models, three omitted-corpus models, final fit, native selection and
cross-fitting traces, per-recording predictions, bootstrap results and receipts
are retained. The final research JSON contains explicit fixed and learned
weights; use the **fixed** procedure for the recommended candidate.

The 181,507-byte research export replays exactly on the local embeddings.
Warm inference over three embeddings takes at most 0.731 ms in 100 runs;
peak fitting/evaluation RSS is 653,266,944 bytes. These measurements exclude the
audio encoder and application startup. All frozen resource bounds pass.

Commands and results:

```sh
./experiments/run.py individual-local --directory experiments/audio_lab/local/relax-617-20261030/local-1
uv run pytest experiments/audio_lab_tools/test_relax_consensus.py -q
./experiments/run.py relax freeze
./experiments/run.py --log audio_lab/local/relax-617-20261030/round-1/run.log relax run
uv run pytest experiments/audio_lab_tools/test_relax_consensus.py -k retained_real -q
uv run pytest experiments/audio_lab_tools/test_relax_replay.py -q
git diff --check
```

The first focused run passed six design checks, with the unfinished real-run
check skipped; that real-run check passed after completion. The separate replay
check also passed, independently reconstructing every outer/transfer prediction
from coefficients and explicit equations (over 10,000 scalar comparisons).
Eight focused checks passed in total. An initial design check caught ordering of
the ManyMusic missing-stratum validation; that was fixed before the frozen run.
No global suite or global-suite test lock was used. The obsolete lock import in
the local snapshot exporter was removed as instructed.

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.

| Artifact | SHA256 |
| --- | --- |
| plan.json | `c37bc4c30e8d42ae34d50a7f3aedebf898e61f61060dd01bc2caf2648abe3cf2` |
| report.json | `af75e91e5bad6a927109f8dd29a2695958f4427fa4e2730db993f7197c66c9dd` |
| evaluation.receipt.json | `0223cfb2d927db82cd02827f162510f8676335c6bdb9a3e833e9f404691ba6f5` |
| models/full.json | `46ca8cc085adaab92eed16fa51b49ac4de311996a8b9492f144a5cf70aec7851` |

Independent review, missing historical acoustic comparisons and any eventual
production integration remain separate. Source notices and research-use terms
carry forward; the research JSON is not a distribution-clearance decision.
