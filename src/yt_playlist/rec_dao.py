"""Backwards-compatible alias. The recommendation DAO now lives in the unified repos/ package
as RecRepo (Repo base + @synchronized), alongside every other domain DAO. Existing call sites
construct `RecDao(store)` — kept working here so nothing had to change at once.
"""
from yt_playlist.repos.rec import RecRepo as RecDao  # noqa: F401
