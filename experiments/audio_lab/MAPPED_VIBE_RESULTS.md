# External attributes followed by fixed vibe mappings

29 October 2026. Research for #530; all implementation and outputs remain under
`experiments/`. The pipeline learns external annotations from our cached audio
analysis and applies fixed rules to the predicted scores. The evaluation applies
the **same rule to held-out external annotations**. Local vibe labels do not
enter fitting, mapping weights, model selection or these quality metrics.

The approach produces useful external rankings, including directly supervised
Focus. The larger per-vibe search does **not** consistently beat the existing
native experts or a shared model configuration. Preserve the two-stage design,
the direct activity head and the useful source-specific alternatives; this round
does not establish a general product-model upgrade.

## Frozen protocol and reproducibility

- Directory: `local/mapped-vibes-20261029/round-1/` relative to this document.
- Plan SHA256: `5d8744ca36ce0037d47f756333e26ba5f04198c0faa1a4b56c9f6e51d49a32dc`.
- Transfer correction: `local/mapped-vibes-20261029/component-repair-1/`, plan
  `d3f2469c6624a96d6eb53397bf523ff1573e487c2bf34409d37ebdf0c666743e`.
  Its `summary.json` supersedes two original transfers and the dependent AMG/NTW
  ensembles. All 272 original evidence files, including the original results and
  source archive, remain unchanged. The other 30 transfer receipts are reused
  by hash; native fits, six-scorer exports and consumer replay are unchanged.
- Thirteen source versions, 9,511 corpus rows, **52 supported dataset/vibe
  pairs**, plus the separate Focus At-work ablation. Corpora overlap; these are
  not 9,511 independent recordings. Missing label support stays unsupported.
  ManyMusic/Memo2496 pilots and alternative Emotify+ annotations retain their
  previous results; the new scorers were not evaluated on those in this freeze.
  The overlapping 4Q source is represented by the expanded MERGE version.
- Three feature families: 27 acoustic attributes, 200-value MusiCNN embeddings,
  and their concatenation. No pretrained mood predictions are used as inputs.
  All retained feature rows have complete acoustic values. No new audio
  extraction was needed.
- Mean/median controls and 19 configurations per feature family: five ridge,
  six random-Fourier-feature ridge, eight nearest-neighbor configurations.
  Thus 59 candidates, independently selected for each supported vibe.
- Five outer group folds; three inner group folds. Known artists, audio and
  recording identities stay grouped. Arrays, inner/outer assignments, exact
  rules, grid, code and numerical runtime were saved before fitting.
- Primary selection: mean inner-fold linear-relevance, tie-aware NDCG over
  `min(10, ceil(0.2 * validation_rows))` tracks. Ties use expected gain across
  every ordering within the tied block. MSE breaks ranking ties, then fixed
  grid order. Nonvarying reference partitions are explicitly unevaluable.
  The shared-configuration comparator maximizes macro NDCG across supported
  main vibes, breaking ties by grid order; it excludes the Focus work ablation.
- Squared loss estimates bounded conditional means / marginal vote or class
  probabilities without binarizing fractions. Independent native-target MSE
  selection is a separate diagnostic. Reports retain MSE, MAE, top-set mean,
  lift over random ranking, Spearman and fold-level results.
- All comparisons below use the same target tracks, references and outer
  folds within each row. The prior native embedding-only MAE selector uses
  its already saved OOF predictions on identical rows/folds. Its full-data
  source models are never used to predict their own validation rows.

Commands through the approved entrypoint:

```sh
./experiments/run.py check
./experiments/run.py mapped-vibes freeze
./experiments/run.py mapped-vibes dataset --dataset cal500-raw-activity --independent-execution
./experiments/run.py --log audio_lab/local/mapped-vibes-20261029/round-1/campaign.log mapped-vibes run --independent-execution
./experiments/run.py mapped-vibes report
```

The user authorized research runs without the machine test lock; focused pytest
still uses the normal bounded lock. A separate round lock prevents concurrent
mutation, completed-stage receipts check every output hash, and one numerical
thread runs the stages sequentially. Cached extraction, shared decompositions
and cached neighbor distances make this a small-model comparison, not another
audio-processing campaign.

Validation: 85 focused research tests passed, followed by successful syntax
validation of all 93 research Python files. After completion, the entrypoint
verified all 47 stage receipts and their 126 output hashes without rerunning
training or consumers. Total recorded stage execution was 668.18 seconds.
Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.

## Focus: Reading and Studying

Reference and prediction both use the equal mean of `Usage-Reading` and
`Usage-Studying`. At-work is evaluated only as a separate three-label reference;
it is not used to alter the main Focus definition or select its winner.

494 tracks, 491 connected groups. Each outer validation partition contributes
its ten highest-ranked tracks. Suitability below means the original human vote
fraction transformed by that fixed mean, not classification accuracy.

| Method | NDCG | Top-set suitability | MSE |
| --- | ---: | ---: | ---: |
| Training mean / random ranking | 0.2285 | 0.1064 | 0.02208 |
| Acoustic attributes, inner-selected | 0.3463 | 0.1842 | 0.02205 |
| Embeddings, inner-selected | 0.4774 | 0.2328 | 0.02017 |
| Combined, inner-selected | 0.4663 | 0.2200 | 0.01982 |
| Per-vibe selection across feature families | 0.4539 | 0.2225 | 0.02020 |
| Shared configuration across source vibes | 0.4730 | 0.2250 | 0.01942 |
| Native attributes selected individually by MSE | 0.4376 | 0.2128 | 0.01997 |
| Prior embedding-only native MAE selector | **0.4877** | 0.2237 | 0.02039 |

The new per-vibe selector beats random ranking in all five outer folds. Direct
activity supervision is useful; broadening model and feature selection did not
improve on the prior native selector. The full-source export selects embedding
ridge with alpha 100 using its own grouped development folds. The At-work
ablation scores 0.4415 against its different reference (random 0.2729); raw
metrics across those two references do not choose a Focus formula.

## Coverage and held-out results for all six vibes

Cells show selected-model NDCG and gain over random ranking **on that same
external reference**. Compare models within a dataset/vibe task; do not rank
datasets by their raw numbers. Baseline offsets and relevance variation differ.

| Dataset | Rows | Groups | Relax | Focus | Cruise | Energize | Uplift | Release |
| --- | ---: | ---: | --- | --- | --- | --- | --- | --- |
| cal500-raw-activity | 494 | 491 | 0.520 (+0.349) | 0.454 (+0.225) | unsupported | 0.334 (+0.181) | unsupported | unsupported |
| emotify-artist | 400 | 138 | 0.778 (+0.127) | unsupported | 0.752 (+0.000) | 0.779 (+0.370) | 0.766 (+0.402) | 0.666 (+0.349) |
| pmemo | 766 | 267 | 0.876 (+0.110) | unsupported | 0.885 (+0.128) | 0.939 (+0.242) | 0.901 (+0.181) | unsupported |
| deam | 1800 | 1796 | 0.828 (+0.101) | unsupported | 0.857 (+0.116) | 0.861 (+0.267) | 0.891 (+0.282) | unsupported |
| vgmidi-original | 76 | 5 | 0.892 (+0.036) | unsupported | 0.876 (-0.003) | 0.902 (+0.090) | 0.879 (+0.066) | unsupported |
| merp-static | 49 | 42 | 0.945 (+0.061) | unsupported | 0.929 (+0.078) | 0.965 (+0.214) | 0.873 (+0.057) | unsupported |
| amg1608-matched | 403 | 309 | 0.886 (+0.105) | unsupported | 0.858 (+0.074) | 0.930 (+0.224) | 0.860 (+0.156) | unsupported |
| ntwicm-matched | 55 | 50 | 0.923 (+0.061) | unsupported | 0.867 (+0.045) | 0.822 (+0.114) | 0.817 (+0.096) | unsupported |
| soundtracks-all | 470 | 295 | 0.845 (+0.286) | unsupported | 0.885 (+0.116) | 0.846 (+0.329) | 0.705 (+0.281) | 0.787 (+0.567) |
| jamendo-human-4shards | 216 | 104 | 0.734 (+0.233) | unsupported | unsupported | 0.772 (+0.567) | 0.578 (+0.167) | 0.608 (+0.386) |
| coimbra-mirex-like | 900 | 664 | unsupported | unsupported | unsupported | 0.590 (+0.403) | 0.484 (+0.302) | 0.694 (+0.514) |
| merge-quadrants | 3549 | 1750 | 0.816 (+0.547) | unsupported | unsupported | 0.986 (+0.482) | 0.979 (+0.464) | unsupported |
| emma-preview-matched | 333 | 257 | 0.882 (+0.134) | unsupported | 0.849 (+0.071) | 0.792 (+0.278) | 0.804 (+0.271) | 0.695 (+0.272) |

51/52 main pairings have positive NDCG gain over random ranking; 46/52 improve
MSE over the native training-mean control. These are descriptive counts, not a
significance claim. The new selector beats the prior native MAE selector on
24/52 pairings and the shared-configuration selector on 18/52. Outer results
are not used to retune or replace any frozen candidate.

Cruise illustrates the limitation: Emotify's gain is only 0.000453, top-set lift
is negative (-0.00197), and only two folds beat random. The same source's
combined-feature-only selector reaches 0.7853, but choosing it after seeing that
outer result would be a new exploratory choice. PMEmo's moderate-arousal and
positive-valence rule has clearer external signal (0.8846 versus 0.7561), but
it is a different reference. This motivates investigating dimensional activation
support for Cruise rather than declaring the Emotify-based export successful.

For Release, the frozen rules use power, aggression or anger directly. They
do not require fast tempo, negative valence, or a normalized competition with
the other five vibes. VA-only sources have no Release mapping. Focus uses only
the direct activity labels. Exact expressions are in the frozen `inputs/rules.json`
and `audio_lab_tools/mapped_vibe_rules.py`.

## Cross-corpus tests and bounded combinations

Thirty-two directions were predeclared: every ordered pair among six VA
sources, plus Emotify/EMMA in both directions. All known target artist, normalized
title, recording-ID and audio-hash matches are removed before fitting a source.
Artist, recording and audio anchors exclude their entire connected source
component, including transitive collaborations. Title-only matches exclude rows
without expanding components; shared franchise labels and empty identities do
not connect unrelated recordings. Preprocessing and selection then
use three grouped **source-only** folds; target annotations supply evaluation
references only. Previous transfer results were already inspected, so these
are new tests of this frozen protocol, not pristine confirmation corpora.

Independent review found that the original row-only exclusion left connected
collaborators in PMEmo training for two targets. The correction removes another
290 rows for AMG1608 (737 to 447 training rows) and 281 for NTWICM (751 to 470).
Both directions repeat source-only selection after exclusion. The correction
audits all 32 directions and recomputes only those two and their dependent
ensembles. Counts below use the corrected summary. This demonstrates a boundary
failure and its repair; it does not measure the amount of bias in the old scores.

31 directions fit successfully. NTWICM to AMG1608 is explicitly skipped:
26 overlapping source tracks are removed, leaving 29, below the predeclared
30-track threshold. It is not counted as a successful model evaluation.

| Vibe | Evaluated directions | Positive NDCG gain | MSE below source-mean control |
| --- | ---: | ---: | ---: |
| Relax | 31 | 23 | 11 |
| Cruise | 31 | 22 | 9 |
| Energize | 31 | 30 | 23 |
| Uplift | 31 | 28 | 17 |
| Release | 2 | 2 | 2 |

For example, PMEmo-trained experts tested on DEAM score 0.8154 versus random
0.7415 for Cruise, 0.6980 versus 0.5938 for Energize, and 0.8608 versus 0.6099
for Uplift. DEAM-trained Energize transfers to PMEmo at 0.9198 versus 0.6967.
Ranking can transfer while absolute scores remain poorly calibrated. Equal
averages of eligible other VA sources are a predeclared bounded combination;
their weights never use target labels. Detailed ensemble results are retained
in the corrected `component-repair-1/summary.json`, alongside every single-source
comparison and the original receipt/prediction hashes for unaffected work.

The six target-specific equal VA ensembles beat random ranking on 21/24
target/vibe tasks. They also retain failures: on VGMIDI, ensemble Relax is
0.8165 versus random 0.8557 and Cruise is 0.8588 versus 0.8781; NTWICM Cruise
is 0.8162 versus 0.8218. Averaging sources is not an automatic improvement.

## Research export and operational check

The six-source assignment was frozen by annotation meaning/provenance before
fitting, not chosen by comparing metrics across different tasks. Alternative
experts from all thirteen datasets remain exported separately.

| Vibe | Predeclared source | Full-source selected model |
| --- | --- | --- |
| Relax | Emotify | Combined attributes/embeddings, ridge alpha 1000 |
| Focus | CAL500 | Embeddings, ridge alpha 100 |
| Cruise | Emotify | Combined, three neighbors, inverse-distance weights |
| Energize | PMEmo | Acoustic attributes, ridge alpha 0.1 |
| Uplift | PMEmo | Embeddings, ridge alpha 100 |
| Release | Jamendo human votes | Embeddings, 64-feature RFF, gamma 0.5, alpha 100 |

`six-scorers.json` is 3,868,003 bytes. Independent serialized replay matches
all thirteen sources exactly in this runtime; independent vibe-score checks
also have zero error. Scoring 494 cached input rows takes 0.00017–0.0702 seconds
per exported scorer, including numerical model materialization. These are Python
measurements, not browser audio extraction or browser latency measurements.

The completed consumer check uses the existing frozen application runtime and
SQL snapshot: 14,149 catalog recordings and 10,278 prepared profiles. There are
77 complete compatible captured recordings; 75 also have the frozen serving
metadata required for replacement observations. It recomputes their external
scores and exercises actual Mix/Radio publication through the research adapter. It drops
local labels and stored prediction tables from the observation input; retained
provider/audio measurements are operational context, never evaluation truth.
No propagated prediction becomes an independent vote.

All 24 Mix Tape lanes contain 12 tracks; all three Radio intensity settings
produce 24-track episodes. The adapter receives 11,256 actual profile reads.
The production shared-intensity rule matches `max(energize, release)` on all
77 compatible captured recordings. No unarchived payload reads occur, the
frozen source database is unchanged, and the frozen runtime hashes match.

The private backup takes 132.46 seconds and schema initialization 32.70 seconds.
Publication takes **227.42 seconds** under the declared 600-second diagnostic
bound; it exceeds the original 180-second target. Mix assembly takes
0.058–0.254 seconds per vibe. These measurements include the research adapter
and existing prepared state; fresh startup, browser execution, default-bound
acceptance and human listening quality are not established by this run.
Complete observations are retained in `consumer.report.json` and the actual
Mix/Radio output JSON files.

## Interpretation and next product candidate

Use source-appropriate attribute heads and explicit independent vibe mappings
as the product candidate architecture. Direct CAL500 Reading/Studying is the
clearest next Focus candidate; retain its older native selector as a comparator.
Allow acoustic attributes for activation and a separate force/aggression expert
for Release. This round does not support making a broad per-vibe winner search
the default or replacing every existing native head.

Cruise needs a better-supported candidate before adoption. Dimensional arousal
ratings express moderate activation more directly than the fraction of listeners
selecting a power label. Cross-corpus ranking and absolute-score calibration
must both be checked on common target rows before selecting source mixtures.
The current predeclared research bundle is reproducible, not a claim that every
anchor source is the best product choice.

Existing excerpt/whole-song alignment differences, missing artist identities,
five VGMIDI franchises, preview alignment, tag-derived labels and unknown
upstream pretraining overlap remain material limits. Jamendo's stated research
terms and other release gates remain recorded in #530/#424. Neither local labels
nor a successful consumer replay resolve those product-adoption requirements.

`*.predictions.json` retains every held-out row, external native observations,
mapped references and method-specific vibe predictions. Its `native_predicted`
field is the independently MSE-selected **native diagnostic**, not the raw
attributes for every ranking-selected vibe method. Exact method selections and
fold evidence are in `*.report.json`; full-source attribute-to-vibe scorers are
in `*.experts.json`. Checksummed NPZ arrays and stage receipts accompany them.
