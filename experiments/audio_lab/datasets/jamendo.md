# MTG-Jamendo human judgments

Four official audio shards (00, 07, 23, 53) yielded 216 recordings with complete
explicit human judgments. Official archive and selected-track SHA256 checks
passed. Seven tasks retain positive-vote fractions: relaxed, happy, aggressive,
party, sad, acoustic and electronic. Missing uploader tags are unknown.
Original split-0 test annotations entered training; use new artist grouping.

Oct 30, #577/#614: the 187-row transfer-protected aggression head became the
selected [Release model](../models/release.md). Adding Emotify power or balancing
native aggression/power/anger tasks did not improve its fixed anger ranking;
the browser power blend had a supported regression. Happy judgments also
contribute to [Uplift](../models/uplift.md).

Nov 1, #709: froze 41 potential tracks across 34 additional artists in shards
70/63, split into 21 training and 20 sequestered evaluation tracks. Both official
endpoints returned HTTP503 for both shards: zero new audio/usable artists.
The extension result is inconclusive, not evidence of failed generalization.
Empty-extension and original-pool refits match exactly; comparison to the older
187-row artifact reflects the 216-row pool difference, not an extension gain.
No held-out aggression values were opened. Preserve the fixed cohort for a
separately bounded retry; no backfill was performed.

Source revision `cafd8e20c265ed84f1e61f1c875327971f43a62f` retains noncommercial
research/academic dataset terms and individual audio licences; product notices
do not establish distribution clearance.
Evidence: `local/individual-20261030/remaining-1/`,
`local/release-614-20261030/round-2/`,
`local/release-extension-709-20261101/round-3/`.
Protocol: [artist extension](../RELEASE_EXTENSION_PLAN.md); retained
[label audit](../RELEASE_LABEL_AUDIT.md) is also a hashed experiment input.
