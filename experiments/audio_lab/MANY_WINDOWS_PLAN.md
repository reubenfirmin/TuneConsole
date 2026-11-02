# All rated ManyMusic windows (#707)

Freeze before extraction or fitting. Reuse the 283 acquired recordings and
their 1,695 original rated thirty-second windows. Preserve the source audit's
repeated-response aggregation, exact intervals, listener disagreement and
missingness. Extract three consecutive ten-second chunks for each interval and
train on the mean embedding with its single thirty-second target; chunks are
not independently labeled observations. Reject incomplete intervals.

Compare one-window and all-eligible-window ridge procedures separately for
natural and generated music. Preserve the original target-independent selected
window for each recording. Both procedures select alpha .1, 1, 10, 100, 1000
per native VA target on identical selected-window validation songs. All-window
training weights each recording equally, including training normalization;
each eligible window gets reciprocal recording-window count. All native
evaluation songs have equal weight. Report additional-window coverage without
claiming additional independent recordings or fresh confirmation.

Construct global connected components using every raw window, all sixteen
prior source versions (original thirteen plus three pilots) and the frozen local identity inventory. Include
recording, artist, generated prompt, declared group, audio hash and conservative
title edges. Exclude known local components. Keep components intact in all
five outer/three inner folds. Hold out every window of validation recordings.
For transfer, exclude every component in the complete destination corpus before
source selection. Use only original Memo pilot recordings; the entire nonpilot
Memo collection is reserved for #710.

Fixed references and mappings are unchanged: VA Relax is mean(1-arousal,
valence), Cruise mean(moderate-arousal, valence), Energize arousal and Uplift
valence. Use the already declared Soundtracks mappings and Memo pilot VA
references for transfer. Evaluate both natural-source and generated-source
experts; neither is selected using transfer outcomes. Report matched native
historical/browser selected-window comparisons, all four fixed-reference
rankings/top-list lifts, native errors and calibration. The exact adopted
four-specialist artifacts are separately replayed on identical rows, with
their native training exposure disclosed. New VA-only experts do not silently
replace the adopted specialist mixtures or change their source weights.

Bootstrap whole components within folds with 1,000 paired draws. A training
procedure recommendation requires at least .02 average NDCG and .01 average
top-list-lift gain over one-window training across the four native mappings in
natural music under both preparations, positive conditional lower NDCG bounds for
the average gain, no more than .01 ranking/lift regression on any supported
native or transfer domain, and no native VA MSE regression above .005. A model
production recommendation additionally requires beating the exact adopted
specialists by .02 NDCG/.01 lift on supported transfer rows. Negative or
inconclusive outcomes are valid; never expand the search after seeing results.

Metadata-only correction before any fits: the 188 natural recordings have
5-10 rated windows each, whereas all 95 generated recordings have exactly one.
Generated music is therefore an unchanged negative control (ranking/lift
differences within 1e-12), not evidence for or against additional-window gains.
No model-comparison outcomes preceded this correction. Round 4 records it and
reuses only verified features from the preserved incomplete earlier rounds.

Resource revision before any fit: the first single-worker extraction was
interrupted after its throughput indicated the two-hour budget was insufficient.
Preserve its plan and chunks. Use four identical workers, each with one numerical
thread, and reuse only checksum/provenance-verified chunks. No intervals, targets,
selection, splits or decision thresholds change. Bound extraction to two hours
and 1 GiB new feature storage; stop on worker/provenance failures.
Snapshot initialization is serialized after round 2 stopped on a shared
manifest staging-file collision before any fit; round 3 preserves that failure
and changes initialization only. Bound fits/evaluation to two hours,
1 GiB Python RSS and 1 MiB per candidate artifact.
Keep research, receipts, temporary PCM and reports under experiments/. Use a
TemporaryDirectory for PCM and clean it on success and failure. Do not acquire
the global-suite lock or alter production files.

Final extraction-only revision, still before any fit: use the previously
validated #424 C++ MusiCNN graph and frontend, two single-thread workers, for
every training window. Do not mix Node features into these features. Verify
all original selected windows against the original cache at the established
0.0005 maximum feature-error bound before fitting. Model weights, patch pooling,
input decoder/intervals, targets, splits and statistical criteria remain fixed.
This supersedes the earlier four-Node-worker resource policy. Earlier attempts
remain preserved, and their compute is disclosed separately from the final run.
