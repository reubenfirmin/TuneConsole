# MERT intermediate layers, #809

Compare final-layer mean with concatenated means of transformer blocks 5 and 6,
using exactly #708 round 3's 288 recordings, 576 ten-second intervals, labels,
global components and outer folds. The paper motivation is
[Kang and Herremans, version 3](https://arxiv.org/html/2502.03979v3).
This is a fixed representation comparison, with no layer sweep or encoder update.

Use the pinned local 95M checkpoint and original Python/Torch environment.
FFmpeg decodes the original interval to 240,000 float32 mono samples at 24 kHz;
apply the published processor's normalization. Hidden state zero is the input
to the transformer stack; states 5 and 6 are the outputs of the fifth and sixth
blocks. Concatenate temporal means in that order (1,536 dimensions). Retain the
final state mean from the same pass and require maximum error at most 1e-6
against every cached #708 final-layer vector. Require exact original PCM hashes.
MusiCNN retains its original 16 kHz/graph preprocessing; MERT retains its 24 kHz
processor path. There is one fixed preprocessing path per encoder, not a new
preprocessing search. Record hidden-state shapes and final-state equivalence.

Freeze this document, entrypoints, imported research sources, original cohort,
feature arrays, adopted-artifact snapshots, source annotations and original
receipts before extraction. Keep all nonpilot Memo excluded. Bound extraction
to two hours, 4 GiB per worker and 256 MiB new features/caches. Retain checked
chunk caches for interrupted resumption; disposable PCM uses TemporaryDirectory
and is removed after success or failure. One CPU inference thread, offline
execution. Three replay calls must reproduce pooled vectors within 1e-6.

Reuse identical nested native ridge selection (constant or alpha .1,1,10,100,
1000), with fold-specific standardization and the existing seed. Fit means of
valid chunks, clip per-chunk native predictions then average, and preserve the
six adopted specialists' fixed mappings and source weights. Exclude each held
global component from all sources before fitting or inner selection. For
source-only transfer omit the destination family (both Many strata together)
and all its components. Never replace a missing fixed expert. Existing final
and MusiCNN nested predictions can be reused only after cohort/feature parity.
Keep exact adopted controls explicitly exposed diagnostics.

Report native MSE/MAE, fixed-mapping NDCG, top-list lift and mapped error; use
1,000 paired component draws. Keep natural/generated strata separate. Primary
cases and advancement gates are identical to #708: each needs 24 rows/12 groups;
mean primary NDCG gain .02 and lift .01; every primary NDCG interval lower bound
positive; no primary loss exceeding .01 NDCG/lift or .005 mapped MSE. Compare
middle layers with final layers as primary and matched MusiCNN as secondary.
No selection on inspected destination results. Fit/evaluation maximum one hour.
Failed or unavailable primary metrics cannot establish advancement.

The cohort has already been explored. Even a successful comparison requires
independent confirmation and deployment evidence. Report extraction latency,
load time, RSS, storage and cached-head replay separately. This task neither
exports a production artifact nor establishes browser feasibility. Any justified
production work belongs to a separate child of #136.

Round 1 completed extraction before a concurrent change to the shared receipt
verifier. Round 2 freezes the current dependency closure before fitting and
explicitly references round 1's plan and extraction receipt. Reuse requires
the same representation protocol and verifies/copies every extraction output;
it does not rerun inference or replace the original resource measurements.
No target comparison had been fitted or inspected when this round was frozen.
