# Private library annotations and captured audio

Source: the developer's TuneConsole account and exported research snapshots.
This is a private, project-specific collection; there is no public dataset URL.
Capture behavior is documented in the [extension reference](../../../extension/README.md).

Each human annotation records acceptable vibe alternatives for a canonical
recording, with revision/supersession history, playback identity, optional sample
ID and feedback-time model identity. Selected vibes are positive; unselected
vibes are unknown. Empty or withdrawn answers remain authoritative across
alternate uploads and recording redirects.

The retained metadata-transfer cohort contains 115 currently positive canonical
recordings resolved from 140 annotation revisions: 85 development identities
and 30 evaluation identities. Those 30 have been evaluated and are no longer
an unused reserve. Of the development identities, 55 trained the shared teacher
and two had prior prediction-label exposure. These populations are unsuitable
as fresh independent confirmation.

Captured measurements are separate records with excerpt positions, sample IDs,
pipeline/model versions and original-recording lineage. A complete capture does
not guarantee a compatible embedding. Provider estimates, teacher predictions
and propagated descendants remain separate from human annotations and original
audio measurements; they do not add human votes. Positive-only labels cannot
establish ranking precision or calibrated suitability probabilities.
