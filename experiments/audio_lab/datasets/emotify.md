# Emotify and Emotify+

Emotify supplies 400 excerpts with nine native **felt-emotion** targets. Join
genre and within-genre filename to global annotation IDs. The initial validation
had recording groups only; recovered audio tags later supplied 138 artist
groups. Preserve known artist connections without claiming unknown identities
are independent. Publisher attribution is embedded in the shared model.

| Date / experiment | Result and decision |
| --- | --- |
| Oct 28, native ridge | Artist-grouped embedding MAE .11303 versus training mean .14503. Listener weighting changed MAE only to .11293; no established improvement. |
| Oct 28, shared product checkpoint | Nine emotion heads plus a six-vibe calibrator gave 38/55 acceptable top choices. Selected as the historical shared model; this reused developer set is exploratory. |
| Oct 29, #530 extensions | Early/center/late windows, listener weights, alternate learners and consensus did not establish a general six-vibe replacement. |
| Oct 30, per-vibe follow-ups | Calmness/inverse tension anchors Relax; power supports Energize; joyful activation supports Uplift. See individual model logs for fixed weights and adoption tradeoffs. |

Emotify+ adds **perceived-emotion** judgments on overlapping Emotify audio:
3,031 CSV rows, 399 file identities and 174 listener IDs. Strict title/genre
consistency retains 265 tracks; deduplicate repeated listener responses.
Grouped embedding MAE .12523 was worse than the .12445 training mean, while
pretrained mood features reached .12312. Retain this negative result. Extra
listeners do not create independent audio; share all recording/artist exclusions
with Emotify. Keep selection frequency separate from conditional intensity.

Evidence: `local/external-20261028/` and its `checkpoint-1/` reports.
Source: [Emotify publisher](https://www2.projects.science.uu.nl/memotion/emotifydata/),
[Emotify+ release](https://data.mendeley.com/datasets/wbk8zr9bd2/1).
Model derivation: [shared](../models/shared.md), [Relax](../models/relax.md),
[Energize](../models/energize.md), [Uplift](../models/uplift.md).
