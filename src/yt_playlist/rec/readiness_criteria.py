"""Evidence required to call onboarding output familiar or usable."""
from yt_playlist.core.audio_vibe_contract import ACOUSTICBRAINZ_ESTIMATE, POOLED_SOURCES

FAMILIAR_SONGS = 2
FAMILIAR_PLAYS = 2
MEASURED_VIBE_SOURCES = ('audio_measurement',)
PROXY_VIBE_SOURCES = (*POOLED_SOURCES, ACOUSTICBRAINZ_ESTIMATE)
VIBE_MINIMUM_SUPPORT = 0
COMPLETED_LASTFM_STATUSES = ('found', 'no_match')
