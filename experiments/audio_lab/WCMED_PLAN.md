# WCMED source-only transfer, #812

Freeze before downloading or scoring. Recover only the 58 exact original
Saarland Western Music source filenames found in the publisher listing.
Do not replace the three unavailable Debussy recordings with another
performance. Retain all 400 metadata rows and every missing/short exclusion.
Download at most one GiB in 30 minutes, at most 64 MiB per recording; one
request per source, twenty-second socket timeout. Keep URL, headers, hash,
byte count, decoded duration and failure details. No publisher audio
checksums are available; local hashes establish subsequent integrity.

The first recovery round completed before scoring: 57 files succeeded and
Schumann Op105-01 hit a TLS handshake timeout. A second prospective round
hashes the complete first acquisition receipt and all retained source files,
reuses their original paths, and requests only the failed recording once.
Keep the combined download bytes within one GiB. No outcomes were opened
between rounds; preserve the first source snapshot, failures and resource
accounting. The second round becomes the evaluation round.

Post-result identity repair: independent checks of round 2 discovered that
the publisher metadata repeats `Chopin_Op031_103_20040204-SMD_8.mp3` with two
different intervals, and omits ranked filename segment 10. Do not guess which
interval belongs to which rank. Round 3 excludes every repeated metadata ID
(both rows), reuses all verified source audio and exact interval features,
and recomputes the same fixed-model evaluation. No model or gate changes.
The prior results are superseded; round 3 is a transparent data repair after
outcome exposure, not a new independent confirmation. Single-thread replay
checks must use the same numerical thread count as prediction.

The source [naming convention and CC BY-NC-SA 3.0 terms](https://www.audiolabs-erlangen.de/resources/MIR/SMD/western-music)
identify anonymous performer IDs. Merge recordings, composer/work families
(strip a final numeric movement suffix), and anonymous performer IDs
transitively. Report component counts before predictions. This is an external
source-only holdout; there is no WCMED fitting or invented train/test split.
Use 1,000-draw whole-component uncertainty; report a recording-cluster
sensitivity because transitive performer/work components may be few.

For each supplied start/stop interval covering ten seconds, extract its
center ten seconds directly from the exact original MP3. Check the supplied
duration arithmetic and decoded source coverage. The README says the
published excerpts have short attack/decay treatment but gives no envelope
parameters. Their processed audio is not in the acquired package. These
reconstructions therefore do not establish sample parity with the annotated
files. Do not fabricate fades. Report this as an unresolved reconstruction
limitation and repeat summaries for intervals at least twelve seconds long
(at least one second of edge margin), without claiming that this proves
fade equivalence. Do not pad or repeat short excerpts.

Use the existing C++ MusiCNN reference pipeline and its exact frozen graph,
200 values, 16 kHz mono float32, three full 187-frame patches, one numerical
thread. Freeze all four adopted specialist artifacts from #708 and the
existing DEAM native VA prior from #710; no production reads during scoring.
Apply native clipping, then fixed specialist mappings and weights. Seal
predictions before opening ranking outcomes. No training, tuning,
calibration, target-driven source choice or new model.

Primary native evidence is Spearman/Kendall association with the two global
human ranks, rank 1 highest. Check supplied converted ratings agree in
direction. Report VA prior and native VA outputs from the adopted Cruise
experts. Rank scores normalized with (400-rank)/399 support only ordinal
evaluation; they are not absolute ratings or original pairwise votes.
Report Uplift/valence and Energize/arousal rankings directly. Cruise and
Relax composite references use the existing fixed VA formulas applied to
normalized ranks; these are explicit exploratory rank-space proxies, since
midpoint and cross-axis distances lack absolute calibration. No Focus or
Release truth. MSE on these conversions cannot establish calibration.

Report NDCG, lift and paired differences between frozen adopted specialists
and the DEAM VA prior, with component intervals. Make no replacement decision
from a reconstructed single-domain transfer alone. A positive association
adds benchmark coverage; it is not evidence for a newly trained model.
Preserve unknown source-composition and encoder-pretraining overlap rather
than asserting cross-source artist independence.

Budget two hours extraction, 2 GiB child RSS, one hour prediction/evaluation,
64 MiB new feature artifacts. All outputs stay under experiments/. No live
database, global-suite lock, nonpilot Memo fitting or listening collection.
