# Memo evaluation-only confirmation (#710)

Freeze before outcomes: 128 filenames from the immutable November reservation,
excluding the old pilot by filename and member CRC/size. Selection sorts the
SHA256 of `memo-confirmation-710-v1/filename`; never use annotation values.
Bound total requested range bytes (including retries) to 1 GiB, four readers
each with 256 MiB, and 30 minutes. ZIP CRC verifies each member; save audio
SHA256 and exact range receipts. Whole-archive MD5 is not established.
Screen acquired audio against all 16 source-version inventories (the original
13 plus the three audio-backed pilots) and protected local
identities, including recording/audio/artist components. Missing artist names
remain unknown. No replacement of rejected recordings.

Use all six nonoverlapping 10-second chunks covering the actual [15,75) human
annotation interval. Historical preparation uses FFmpeg. Browser preparation
decodes the original MP3 at 44.1 kHz and separately renders each interval at
16 kHz mono; acoustic features use the original 44.1 kHz channel samples.
No padding, normalization or browser surrogate. Use previously validated C++
MusiCNN with frozen graph and exact production acoustic modules; verify acoustic
parity on an old clip first. Two workers, two hours, 1 GiB RSS per process.
Record largest-child RSS, not an unmeasured aggregate claim.

Freeze exact adopted Cruise/Relax/Uplift/Energize artifacts, shared six-label
artifact and named priors. Cruise prior is the original `remaining-1` DEAM
native VA head. Other priors are the unchanged named Emotify combined Relax,
PMEmo embedding Uplift and PMEmo attributes Energize models. Native clipping
per chunk, then mean native values, then fixed mappings. Shared control uses
mean embedding followed by the exact two-stage prediction and normalization.
No fits, mixture choices, calibration or winner selection.

Seal predictions before opening selected songs' valence/arousal trajectories.
Require every timestamp from seconds 15 through 74 inclusive; blanks remain
missing. Mean normalized native VA, then apply the already declared VA Cruise,
Relax, Uplift and Energize references. No Focus/Release reference. Report every
model on identical complete rows, native VA MSE/MAE separately from mapped
NDCG, top lift, mapped MSE and calibration. Non-VA native accuracy unsupported.

One external holdout with paired 1,000-draw component bootstrap. At least 80
eligible recordings and 60 components for a supported conclusion. Confirmation
requires current over both prior/shared controls by NDCG at least .02, top lift
at least .01, positive NDCG lower bound, no regression exceeding .01 NDCG/lift
or .005 mapped MSE. Otherwise regression or inconclusive, per preparation and
vibe. Absolute score error against competing shared probabilities is diagnostic.
This is same-corpus recording confirmation, with unresolved artist independence
and encoder pretraining overlap. Mark all outcomes exposed for future work.
