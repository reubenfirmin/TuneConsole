# Focus chroma confirmation, #815

Freeze before extracting the larger cohort's chroma, fitting or evaluating.
The original #810 result used 32 CAL500 recordings and 31 identity components.
Its embedding control was a small-cohort refit, so the reported +.589 NDCG was
not a measured gain over the installed production model.

First acquire and audit independent human activity references. Bound metadata
downloads at 2 MiB each, 20-second socket timeouts; retain URLs, final redirects,
SHA256, publisher MD5 where available, and failures. Do not submit access forms
or collect listening judgments. Inspect actual native questions and distinguish
missing judgments, unselected checkboxes, weak playlist associations, session
outcomes and algorithmic predictions. Screen candidate recordings and artists
against the eleven #576 inventories and all eight #615 source inventories.
Missing artist identity or unverified rated audio windows prevent an independent
claim. Every nonpilot Memo recording remains reserved and excluded.

The acquired HumMusQA metadata has one studying/working question. Its artist
is in previously inspected ManyMusic data, and the remaining questions do not
provide Reading/Studying suitability labels. PepMusic requires a data request
and bundles studying with unrelated activities. Music in Use's public OSF files
endpoint returns 401. The recovered brain-focus repository has session outcomes
but no stimulus audio. The existing 7digital annotations still lack verified
recording and rated-window alignment. Consequently the current independent
confirmation gate is **unmet**, regardless of the development result below.
Preserve the eligibility audit, do not manufacture negatives for unasked tasks.

Run a larger development diagnostic using the exact 494 raw CAL500 Reading and
Studying fractions, 483 connected identity components, five outer folds and
three inner folds from #615/#711. Keep the 450 common comparison rows as the
primary development comparison. Also report all 494 and the rows outside the
original #810 pilot's connected components. Those rows are still development
exposed; none of these analyses is independent confirmation.

Freeze the exact installed Focus heads and require numerical equality with the
accepted #615 full export. Compare three feature sets: the unchanged historical
200-dimensional MusiCNN vector, the fixed 72-dimensional #810 CQT summaries,
and their 272-dimensional concatenation. Use the original center ten-second
interval, exact source audio SHA256 and 16-kHz mono float32 decode. Re-extract
all chroma and require all 32 shared first-interval features to replay #810
within 1e-12. Keep the pinned private Python 3.12/Librosa environment and full
dependency lock; record worker versions, PCM hashes, latency, RSS and bytes.
No new extraction algorithm, feature selection, pitch rotation or window tuning.
Browser chroma parity remains unmeasured in this stage.

Use the **complete incumbent direct-head recipe** in each protected outer fold:
training-only median imputation and scaling; inner grouped selection between a
training-mean constant and ridge alpha .1, 1, 10, 100, 1000 by native squared
error, independently for Reading and Studying; refit only outer training rows;
clip native predictions to [0,1]; fixed equal native average for Focus. The
embedding control must replay all historical #615 direct predictions within
1e-12. No auxiliary fitting, calibration, selection or outer-test leakage.
Also retain outer training means and the exact installed full heads. The latter
are exposed to these songs and are a diagnostic comparison only.

Report native Reading and Studying squared error, ranking and calibration
separately, fixed Focus NDCG/top lift/calibration, each outer fold, and 1,000
paired component bootstrap draws within outer folds. These intervals condition
on the selected fits and folds; they do not account for repeated unidentified
listeners, previous experiments or domain shift. Preserve original raw checkbox
support and missingness audit. Whole-song suitability votes evaluated on short
excerpts do not establish a benefit to concentration.

Development checks, fixed before outcomes: combined versus protected incumbent
recipe has mean fold NDCG gain at least .02, top lift gain at least .01, positive
95% conditional NDCG lower bound, and neither native MSE increases by over .01.
Compared with the exposed full incumbent, neither NDCG nor lift may drop by over
.01. Chroma alone is diagnostic and cannot replace the primary candidate after
seeing results. Report every failed check. A passing development result alone
does not authorize adoption: require a separately frozen adequate independent
human activity comparison with artist/recording/window audits before starting
the ticket's browser parity, preparation, storage and production work.

Extraction budget 30 minutes, 2 GiB worker RSS, 8 MiB feature artifact; fitting
and evaluation together 30 minutes, 2 GiB RSS. Numerical work uses one thread.
PCM lives in a TemporaryDirectory cleaned on success and failure. No full test
suite or global lock. Run focused actual-data checks of source eligibility,
group boundaries, train-only preprocessing, independent exported-form replay,
accepted incumbent replay, support reconstruction and reported decisions.
Retain the frozen source closure, copied inputs, hashes and seven stage receipts.
No production export, commit or push is part of this development diagnostic.
