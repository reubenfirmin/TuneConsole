# Count-aware Focus results (#711)

1 November 2026. **Retain the adopted direct-ridge procedure.** Both predeclared
count-likelihood procedures improve binomial likelihood but lose Focus ranking
and top-list lift. Neither meets the frozen recommendation criteria; no
production follow-up is supported.

## Frozen comparison

[Protocol](FOCUS_COUNTS_PLAN.md). Round:
`local/focus-counts-711-20261101/round-1/`. Plan SHA256:
`16a09ff1f732965d853c4c481c6231e2914bdb92b47373a363b013a93c59a697`.
The 494-song input, 450-song common comparison, 200-dimensional MusiCNN
representation and corrected #615 global components/folds remain unchanged.
Historical embeddings train every procedure; the same fits score historical
and browser inputs. Each activity selects regularization and, where applicable,
dispersion inside three grouped training folds. Five outer folds produce every
unbiased evaluation prediction. The matched ridge reproduces the accepted
#615 direct predictions within 1e-12 for both preparations.

The raw audit reconstructs all 1,677 responses and both original activity
fractions exactly. CAL500's original Usage fields are checkboxes: omitted fields
in completed Annotation records mean unselected under the established raw task.
Explicit unknown values remain missing. No explicit unknown activity values
occur in this cohort. Six songs have two responses, 350 have three, 112 have
four, and 26 have more. Only 42 responses contain nonempty listener IDs, covering
13 IDs; response totals are not counts of independently identified people.
Beta-binomial dispersion does not resolve unidentified cross-song dependence.

## Held-out Focus results

Metrics average over the same five outer folds. Lift is selected top-list
reference mean minus the fold reference mean.

| Preparation | Procedure | NDCG | Top-list lift | Focus MSE |
|---|---|---:|---:|---:|
| Historical | Matched adopted ridge | .49792 | .11496 | .018854 |
| Historical | Binomial | .46068 | .09629 | .019050 |
| Historical | Beta-binomial | .44771 | .09629 | .019165 |
| Historical | Training mean | .22841 | .00000 | .021727 |
| Browser | Matched adopted ridge | .49860 | .11796 | .018822 |
| Browser | Binomial | .48120 | .11129 | .019033 |
| Browser | Beta-binomial | .45485 | .10296 | .019153 |
| Browser | Training mean | .22841 | .00000 | .021727 |

Paired NDCG differences against matched ridge, with 1,000 whole-component
bootstrap draws within outer folds:

| Preparation | Procedure | Difference | Conditional 95% interval |
|---|---|---:|---|
| Historical | Binomial | -.03724 | [-.07670, .01727] |
| Historical | Beta-binomial | -.05021 | [-.08694, .01735] |
| Browser | Binomial | -.01740 | [-.07700, .02404] |
| Browser | Beta-binomial | -.04375 | [-.09307, .01976] |

Both procedures fail the required .02 NDCG/.01 lift gains in both preparations
and positive lower NDCG bounds. Both remain within the .01 per-activity MSE
regression ceiling. Every beta-binomial outer head selects concentration 19,
the least dispersed predeclared beta-binomial choice. This is not evidence for
extending the searched grid after observing the results.

For historical Reading/Studying, mean binomial negative log likelihood changes
from ridge [.73734, .79583] to binomial [.65505, .76240] and beta-binomial
[.65836, .76275]. Better likelihood is distinct from recommendation ranking.
`report.json` includes separate task MSE/MAE, both count likelihoods, per-task
ranking/calibration, paired native loss uncertainty and descriptive 2/3/4+
response support strata. More responses do not establish better judgments.

## Exact adopted artifact and resources

The exact installed full-fit heads match the accepted #615 export. Their Focus
NDCG is .55599 historical / .54090 browser, but those heads trained on these
songs. These values are explicitly training-exposed replay controls, not
unbiased estimates. The paired ridge refit supplies the unbiased comparison.

Peak RSS was 560,680,960 bytes. Outer model bundles are 74,592–74,892 bytes,
below the declared 1 MiB model budget. Freeze-to-report elapsed time was
16.5 seconds, below the two-hour bound; this includes pre-fit idle time and
evaluation, and excludes the previously completed audio extraction. Fits use
one native numerical thread. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1,
threadpoolctl 3.7.0. Six stage receipts verify archived code, input references,
prediction identities and output hashes. The new Memo reservation is pinned
and excluded from this experiment.

## Validation and limits

```text
./experiments/run.py focus-counts freeze
./experiments/run.py focus-counts run
./experiments/run.py focus-counts verify
./experiments/run.py check --test test_focus_counts.py
```

The completed focused check reports **8 passed**. Checks independently compare
likelihoods with SciPy's discrete probability laws, finite-difference both
penalized gradients, distinguish native unchecked fields from explicit missing
values, and recalculate saved native MSEs/count fractions. Execution checks
outer/inner component isolation and exact accepted ridge replay. All optimizer
fits converge within the fixed 400-iteration ceiling.

This is author evidence for independent review. Reused songs, sparse listener
identities, whole-song activity labels and ten-second excerpts limit inference.
The conditional intervals do not include new-dataset uncertainty or establish
concentration benefit. No new listening collection, production edit, live
database test, global-suite lock, commit or push was used.

Source-provenance correction found during #708: the original freezer captured
imported helpers but omitted this CLI's `__main__` entrypoint. The current
`focus_count_experiment.py` is separately preserved under `postrun-source/`,
with its hash, capture time and original plan hash in `postrun-source.json`.
This is post-run reproduction code, not a prospective entrypoint hash. Original
plans, archived helpers, inputs and measured outputs remain unchanged. The
freezer is fixed for future rounds. Verification now covers seven receipts,
including the additional post-run source record; the source-freeze gap remains
an explicit limitation for independent review.
