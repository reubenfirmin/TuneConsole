# Count-aware Focus comparison (#711)

Freeze this protocol before fitting. Use original CAL500 Reading/Studying yes
and total original checkbox responses, retaining explicit unknown responses as
missing. Preserve the existing raw-task encoding: omitted Usage checkboxes in
PageStatus=Annotation records mean unselected. They are not missing activity
ratings, and no local unselected vibe label becomes a negative.
Reconstruct the original fractions exactly, retaining the same 494-song input,
450-song common evaluation, MusiCNN embeddings and corrected #615 global folds.
No task splitting, new representation or personalization enters this comparison.

Compare exactly two count-aware procedures: penalized binomial logit and
beta-binomial logit with a shared fixed concentration per task selected from
1, 4, 19. Each independently selects coefficient penalty 1, 10, 100, 1000.
The objective is summed song count negative log likelihood plus alpha/2 times
the squared standardized coefficients; the intercept is unpenalized. Each song
has one likelihood term. Binomial responses are a working likelihood, not a
claim of independent participants; beta-binomial permits within-song dispersion
but cannot resolve unidentified listeners across songs. Original response IDs
and missingness must be audited explicitly. Select each task by mean inner
song likelihood using three grouped folds; standardize only training inputs.
Use bounded L-BFGS with at most 400 iterations and require optimizer success.

Match the adopted historical-training procedure: fit historical mean embeddings,
then evaluate both historical and browser excerpts. Keep the exact adopted
full-fit artifact as a separately identified, training-exposed replay control.
The unbiased comparator is the matched ridge/mean refit on the same corrected
outer folds. Verify it reproduces #615's direct held-out procedure. Ridge selects
constant or alpha .1, 1, 10, 100, 1000 by inner song fraction MSE. Include the
outer training mean as another control. Focus is always the equal mean of the
two activity probabilities; clip per excerpt before averaging as applicable.

Report song fraction MSE/MAE, binomial and selected beta-binomial likelihood,
fixed Focus ranking, top-list lift, calibration and descriptive 2/3/4+ response
support strata. Report native-task metrics separately. Bootstrap whole corrected
components within outer folds, 1,000 paired draws, conditional on this reused
population and frozen fits. Also bootstrap paired per-song native losses.

A recommendation requires at least .02 Focus NDCG and .01 top-list-lift gain
over the matched adopted procedure in BOTH preparations, positive lower paired
95% NDCG bounds, no more than .01 MSE regression on either activity in either
preparation, and no more than .01 Focus NDCG or top-lift loss against the exact
adopted artifact. Likelihood alone is insufficient. Assess both predeclared
procedures, disclosing multiplicity; no post-hoc candidate or threshold changes.
Negative/inconclusive outcomes complete the research. Bound fits to one native
thread, two hours, 1 GiB RSS, and 1 MiB per exported model. Keep all new code,
data, receipts and checks under experiments/. Reserve every new Memo recording
for #710. No production change or new listening judgments are authorized.
