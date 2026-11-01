"""Vocabulary for internal development annotations collected by the vibe popup.

Human labels are training/evaluation data for developing the audio-to-vibe model.
They are not product preferences, per-track vibe overrides, or direct inputs to
recommendation ranking and artist/genre propagation. Those consumers must use
audio measurements and model-derived scores. Raw labels belong only in the
development collection/inspection and model training/evaluation paths.
"""

TAXONOMY_VERSION = 'audio-session-targets-1'
PREDICTION_VERSION = 'measured-vibes-1/subgenres-only-1'

# Internal measurement channels, not additional user-facing vibes. Preserve
# comparable shared scores and their propagation lineage for radio intensity
# when independent specialists supply selected-vibe recommendation ranking.
INTENSITY_RELEASE = 'shared_release'
INTENSITY_ENERGIZE = 'shared_energize'
INTENSITY_CHANNELS = {INTENSITY_RELEASE: 'release', INTENSITY_ENERGIZE: 'energize'}
