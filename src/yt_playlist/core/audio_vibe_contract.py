"""Vocabulary for internal development annotations collected by the vibe popup.

Human labels are training/evaluation data for developing the audio-to-vibe model.
They are not product preferences, per-track vibe overrides, or direct inputs to
recommendation ranking and artist/genre propagation. Those consumers must use
audio measurements and model-derived scores. Raw labels belong only in the
development collection/inspection and model training/evaluation paths.
"""

TAXONOMY_VERSION = 'audio-session-targets-1'
PREDICTION_VERSION = 'measured-vibes-3/specialists-1/subgenres-only-1'
ACOUSTICBRAINZ_ESTIMATE = 'acousticbrainz_estimate'
POOLED_SOURCES = ('artist_genre', 'artist', 'genre')
JOINT_AUDIO_POLICY = 'sampled-audio-only-1'
EXCERPT_SUPPORT_POLICY = 'excerpt-fraction-1'

# Internal measurement channels, not additional user-facing vibes. New identities
# prevent retained shared-classifier fits from entering the specialist ensemble.
# Historical measurements and their immutable lineage remain readable.
INTENSITY_RELEASE = 'specialist_force'
INTENSITY_ENERGIZE = 'specialist_activation'
INTENSITY_CHANNELS = {INTENSITY_RELEASE: 'release', INTENSITY_ENERGIZE: 'energize'}


def fit_family_prefix():
    return 'recording-vibes-1/' + PREDICTION_VERSION + '/'


def score_version_filter(alias=''):
    """SQL filter parametrized by measurement, Bayesian and AcousticBrainz versions."""
    version = alias + 'inputs_version'
    source = alias + 'source'
    return (f"(({source}='audio_measurement' AND {version} GLOB ?) "
            f"OR ({alias}prediction_id IS NOT NULL AND {version} GLOB ?) "
            f"OR ({source}='{ACOUSTICBRAINZ_ESTIMATE}' AND {version} GLOB ?))")


def served_score(alias):
    """Keep the served view's estimate precedence if both branches return a row."""
    return (f"COALESCE(MAX({alias}.score) FILTER (WHERE {alias}.source='{ACOUSTICBRAINZ_ESTIMATE}'),"
            f" MAX({alias}.score))")
