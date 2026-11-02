# One alternative representation, #708

Official sources: [MERT implementation](https://github.com/yizhilll/MERT) and
[MERT-v1-95M model card](https://huggingface.co/m-a-p/MERT-v1-95M). The official
implementation requires Transformers 4.38 compatibility. Pin revision
`12af15fef9d0ac838c3f475bfbbf26d2060dd4f5`, its 377,552,987-byte weights,
published LFS SHA256 and every configuration/custom-code asset. Model card:
95M parameters, 20k pretraining hours, 12 layers, 768 dimensions, 24 kHz.
Recording-level pretraining overlap is unknown. Weights CC-BY-NC-4.0;
implementation Apache-2.0. Private Python 3.12, Torch 2.6 CPU, Transformers
4.38.2, NumPy 1.26.4 environment and caches stay under `local/encoder-708/`.
Downloaded custom source is inspected before use; runtime is offline/local-only.

Before extraction, freeze up to 32 recordings per nine already acquired source
versions, at most two per joined component per source, sorted SHA256 of
`encoder-708-v1/source/id`. Protect local components through the complete
16-source-version/local identity join (original thirteen plus three pilots).
Preserve native/generated ManyMusic separately.
Use only the old Memo pilot; all nonpilot Memo, including #710, is excluded.
Preserve existing complete native annotations and exact interval identities:
one original ten-second excerpt for the six established sources, all three
chunks of the selected rated ManyMusic window, six chunks covering Memo [15,75).
No new labels, listening collection or live database. No backfill after freeze.

MusiCNN: exact pinned 200-value graph and previously validated native extractor,
FFmpeg 16 kHz mono. The pinned acquisition manifest identifies
`msd-musicnn-1`, SHA256
`cdea0722bcee7f731286843f2233e3aa69887bb5c3e2dce011eff55f38d04f3e`.
Its [official metadata](https://essentia.upf.edu/models/autotagging/msd/msd-musicnn-1.json)
describes 200k Million Song Dataset audio previews and fifty-tag supervision.
Recording-level overlap with these corpora is unresolved for both encoders.
MERT: the same original interval decoded by FFmpeg at 24 kHz
mono, published processor's waveform normalization, float32 CPU eval mode,
one thread, last transformer layer mean over time. No layer sweep, learned
pooling, dimensionality reduction or encoder update. Save PCM/feature hashes.
Stop after two extraction hours, 4 GiB RSS per process, 256 MiB features or
4 GiB private environment/checkpoint/cache. Checkpoint acquisition is bounded
to 512 MiB / 30 minutes; dependency setup to 30 minutes. Clean temporary PCM
on success/failure. Record full decode/extraction wall time, actual worker
inference distribution, load time, memory, storage, three encoder replays and
separate cached-head replay. No browser feasibility inference from CPU wins.

Use identical constant/ridge alpha {.1,1,10,100,1000} head selection, independently
per native target. Mean chunk vectors for training; clip native predictions per
chunk then average before fixed mappings. Inner selection evaluates that same
aggregation. Normalization is trained inside each fold. Five globally joined
outer component folds, three source-native inner component folds. Exclude every
outer-held component from every source before selection. Also omit each full
destination family (both Many strata together), excluding all destination
components from all remaining sources before selection. Do not substitute an
expert when all fixed experts are omitted. Native targets include CAL500
Reading/Studying, Jamendo aggression, VA, soundtrack emotion and Emotify emotion.

Keep exact current specialist mixture ratios and mappings. Source omissions
renormalize remaining ratios. Report every supported source/vibe pair, native
MSE/MAE separately, mapped NDCG/top lift/error/calibration and 1,000 paired
component draws. Current production artifacts remain exact controls, explicitly
potentially fitted on these exposed rows. They are diagnostics; controlled
encoder comparisons use matched nested heads. Source omission is one external
holdout; do not invent small folds for it. Natural/generated results stay separate.

Primary for the four VA-compatible vibes: Soundtracks, both ManyMusic strata
and old Memo, each grouped OOF and source-family omission. Focus: direct CAL500
OOF. Release: direct Jamendo OOF and full Soundtracks omission against anger.
Require at least 24 rows / 12 components per primary case. Per-vibe advancement
requires mean primary NDCG gain .02, lift .01, positive NDCG lower bound for
every primary case, and no primary case losing more than .01 NDCG/lift or .005
mapped MSE. Otherwise retain. Fit/evaluation bound one hour. Small cohorts can
be inconclusive; constant-label cases keep unavailable ranking metrics and
cannot establish advancement. No model choice based on inspected destination targets.
Any quality advance still needs measured deployment feasibility and a separate
future production task; this experiment never replaces the 200-value pipeline.

Round 1 extracts features before any head fitting/evaluation. The original
freezer omitted the main command module; round 2's reuse check detected this
before copying or fitting. Preserve both rounds. Round 3 fixes the freezer and
archives the entrypoint, unchanged holdout metric helper and investigation checks
before fitting/evaluation. Reuse requires verified receipts and identical
cohort/inputs/workers/policies. Original extraction entrypoint AST equivalence
cannot be established and is not claimed. Before fitting, require fresh PCM
hash/row-order checks for every interval plus MusiCNN parity with original
reference vectors. Account for the full measured extraction cost and additional
audit/copy time; feature copying makes zero encoder calls.
