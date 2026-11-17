"""Vocabulary for internal development annotations collected by the vibe popup.

Human labels are training/evaluation data for developing the audio-to-vibe model.
They are not product preferences, per-track vibe overrides, or direct inputs to
recommendation ranking and artist/genre propagation. Those consumers must use
audio measurements and model-derived scores. Raw labels belong only in the
development collection/inspection and model training/evaluation paths.
"""

TAXONOMY_VERSION = 'audio-session-targets-1'
PREDICTION_VERSION = 'measured-vibes-2/specialists-1/subgenres-only-1'

# Internal measurement channels, not additional user-facing vibes. New identities
# prevent retained shared-classifier fits from entering the specialist ensemble.
# Historical measurements and their immutable lineage remain readable.
INTENSITY_RELEASE = 'specialist_force'
INTENSITY_ENERGIZE = 'specialist_activation'
INTENSITY_CHANNELS = {INTENSITY_RELEASE: 'release', INTENSITY_ENERGIZE: 'energize'}


def serving_prefix():
    return 'recording-vibes-1/' + PREDICTION_VERSION + '/'


def serving_compatible(version):
    return isinstance(version, str) and version.startswith(serving_prefix())
