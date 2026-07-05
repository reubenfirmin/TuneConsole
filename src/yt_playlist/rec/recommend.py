"""Local recommendation logic. Pure functions over a Store (no web imports).

Formerly one ~1240-line module; split by concern into focused siblings (scoring, taste_analysis,
surfaces, recipes, graduation, actions; low-level track ordering lives in ordering.py). This module
re-exports the public API so existing `recommend.X` callers keep working, but new code should import
the specific module. The home cards now have canonical names too: wheelhouse (= for_you) and catalog
(= explore_for_you), so code vocabulary matches the product wording."""
# Module attributes some callers reach through (e.g. recommend.genre_map.family, recommend.transient).
from yt_playlist.util import genre_map  # noqa: F401
from yt_playlist.rec import journeys, transient  # noqa: F401
from yt_playlist.rec.ordering import _field, attach_genres, dj_order  # noqa: F401
from yt_playlist.rec.scoring import (  # noqa: F401
    PlaylistTaste, playlist_taste, genre_adjusted_scores, axis_adjusted_scores,
    _breadth_factors, _axis_weights_for, _apply_axis_weights, discovery_facet_weight,
    genre_distance_fn, _apply_mood, mood_tilt, MOOD_ALPHA,
    BREADTH_FACTOR_MIN, BREADTH_FACTOR_MAX, _NORM_EPS)
from yt_playlist.rec.taste_analysis import (  # noqa: F401
    era_distribution, taste_fingerprint, playlist_facets, playlist_mood_state,
    track_mood_states, taste_breadth, palette, playlist_genre_diversity)
from yt_playlist.rec.surfaces import (  # noqa: F401
    ForYouItem, rotate_sample, rotate_page, for_you, comfort_listening,
    rediscover_playlists, rediscover_albums, taste_sample,
    explore_for_you, complete_playlist, related_artist_suggestions, _rotation_reason,
    wheelhouse, catalog)
from yt_playlist.rec.recipes import (  # noqa: F401
    roll_recipe, cluster_recipe, theme_filter, versioned_title)
from yt_playlist.rec.graduation import (  # noqa: F401
    apply_dislikes, graduate_facet, graduate_moods, graduate_slider_exposure, graduate_play_exposure)
from yt_playlist.rec.actions import (  # noqa: F401
    SyncStatus, sync_status, ActionItem, take_action, refresh_cleanup,
    SYNC_STALE_S, CLEANUP_SURFACE)
from yt_playlist.util.duration import ago as _ago  # noqa: F401  (back-compat: recommend._ago)
