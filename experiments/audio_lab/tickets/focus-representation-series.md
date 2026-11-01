Parent: #530. Follows #615. Priority: **P2**. **In review**. The bounded research series is complete; independent acceptance is pending. Production adoption remains deferred.

Question: does compressing the audio embedding into nine predicted emotions discard information useful for Reading/Studying?

Freeze the same 494 CAL500 rows, 450 common evaluation rows and corrected global component folds from #615. Fit all nine native Emotify heads with complete cross-source exclusions. Compare three procedures: emotion-only, the embedding residual orthogonal to the native emotion slopes, and emotion plus that residual. Controls: exact direct baseline, unclipped emotion predictions, nine-dimensional PCA, fixed random nine-dimensional projection and training mean. Select maps with the same bounded grouped native-MSE grid and nested cross-fitting. Keep Reading/Studying unchanged and both historical/browser preparations separate.

Report ranking, top-list suitability, native error, paired uncertainty and preprocessing/identity traces. Distinguish compression, clipping, regularization and source-transfer effects; a gain is evidence about this representation, not a causal statement about human emotions. Preserve all controls and failures. No production change, new listening gate or selection from an inspected outer winner.

Acceptance: reproducible frozen experiment, independent replay/identity/metric checks, explicit answer about residual information and limitations. Keep #615's exact direct export as the potential production candidate unless a separately authorized adoption decision changes it.

## Completed author evidence

Plan: `experiments/audio_lab/FOCUS_CONTROLS_PLAN.md`. Report: `experiments/audio_lab/FOCUS_CONTROLS_RESULTS.md`. Frozen root: `experiments/audio_lab/local/focus-controls-637-638-20261030/round-1/`; plan SHA256 `4417d96df39c28577f061a463ee1d8bc4d625d7e2d596e807bce1f44e31c5c0f`.

On the unchanged 450 evaluation songs, browser Focus NDCG is 0.432874 for emotions, 0.486086 residual, 0.512183 combined, 0.434409 unclipped emotions, 0.498598 direct, 0.523477 PCA9, 0.463951 random9 and 0.228405 mean. Combined versus emotion is +0.079309 [-0.039471, 0.166253]; the frozen positive-interval criterion is not met. Residual versus mean is +0.257681 [0.173964, 0.362488], with lower MSE. Useful predictive signal exists outside the fitted linear emotion directions, but an incremental ranking win is not established. Orthogonality is not statistical independence. Direct and PCA have lower native/Focus error than the emotion map; PCA is not adopted from an inspected outer ranking.

All five native slope matrices have rank nine. Complete nested global exclusions pass (80 checks); direct predictions exactly reproduce #615. Eight focused checks pass. A separate post-fit arithmetic implementation replays models within 2.09e-16 and independently matches 180 metrics within 1.12e-16. `./experiments/run.py focus-controls verify` passes. Full historical/browser, Reading/Studying, top-list, error and uncertainty results are retained, not just the best score.

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0, one numerical thread. Experiments and focused checks used no global-suite lock. Evaluation SHA256 `a1472c01d38eb3f32df727c38e56e64b1b24189b8b38a556c014c39797352a55`. Source/input/output receipts and the independent-audit receipt are in the frozen root. These are retained author workspace artifacts, with no commit/push or independent reviewer acceptance. Reused labels, source transfer, cross-fit variation, regularization and short-excerpt/whole-song mismatch remain limits. No production changes; #615's direct export remains the potential candidate.
