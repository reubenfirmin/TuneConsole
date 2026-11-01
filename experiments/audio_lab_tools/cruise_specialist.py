"""Research compatibility entrypoint for the production Cruise scorer.

Historical packages retain their archived runtime. New experiments use the same
inference implementation as the application.
"""
from yt_playlist.rec import audio_vibe_cruise as production
from yt_playlist.rec.audio_vibe_cruise import (
    FORMAT, MAX_BYTES, MAX_PAYLOAD, MAX_EXCERPTS, SOURCES,
    audio_embedding, shared, load, score, route, route_samples,
)
