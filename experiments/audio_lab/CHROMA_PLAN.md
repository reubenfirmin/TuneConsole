# Chroma complement, #810

Freeze before extracting features or fitting. Reuse #708 round 3's exact 288
recordings, 265 components, 576 ten-second intervals, outer folds, labels,
nested native ridge choices and fixed specialist mappings. Compare MusiCNN
alone (200 dimensions), chroma alone (72), and their concatenation (272).
Do not choose features, transformations or mappings after seeing results.
MERT is not a candidate in this study. The motivating paper uses Jukebox/MLP;
this bounded study tests its harmonic-information hypothesis with our controls,
not an exact replication: https://arxiv.org/html/2510.04688v1.

Decode the identical rated intervals to 160,000 mono float32 samples at 16 kHz,
and require the original #708 PCM hashes. Preserve original MusiCNN features
exactly. Use private Python 3.12, NumPy 1.26.4, SciPy 1.14.1, Librosa 0.11.0,
Numba 0.61.2, single-thread numerical execution, with a full dependency lock.
Use librosa.feature.chroma_cqt: hop_length=512, fmin=C1=32.70319566257483 Hz,
n_chroma=12, n_octaves=7, bins_per_octave=36, tuning=0, norm=infinity,
threshold=0, cqt_mode=full. For original chroma, np.diff(order=1), and
np.diff(order=2) along time, concatenate each bin's temporal mean then population
standard deviation, in that order. No tuning estimation, data augmentation,
chromatic rotation, per-dataset normalization or target-dependent preprocessing.
Scale features only inside the same training folds as the embedding control.

All nonpilot Memo recordings stay excluded. Preserve natural/generated strata.
Omit destination families and their global connected components from every
source before source-only training, including inner selection. Fixed mappings,
source weights, native clipping and chunk averaging are inherited unchanged.
Reuse only verified baseline predictions, features, inputs and source receipts.

Both candidates use the #708/#809 gates separately against MusiCNN: at least
24 rows and 12 components per primary case; mean primary NDCG +.02, lift +.01;
every primary NDCG component interval lower bound positive; no primary loss
over .01 NDCG/lift or MSE increase over .005. Use 1,000 paired group draws.
Missing primary metrics cannot establish advancement. Report every native
MSE/MAE, ranking/lift, source-only result, domain regression and uncertainty.
No selection or advancement based on the secondary chroma-only diagnostic.

Environment setup budget 15 minutes/512 MiB downloads. Extraction maximum one
hour, 2 GiB RSS and 64 MiB features/caches; fit/evaluate maximum one hour/4 GiB.
PCM uses TemporaryDirectory and is cleaned on success/failure. Three first-
interval replays must agree within 1e-6. Record cold compilation, warm latency,
RSS, environment/storage and head replay separately. Browser feasibility is
unmeasured. This exposed development cohort cannot supply independent
confirmation, and emotion/activity associations do not establish listening
benefits. Export no production artifact; justified production work needs its
own future child of #136.
