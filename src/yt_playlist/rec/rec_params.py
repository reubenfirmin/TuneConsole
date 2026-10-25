"""Registry of every user-tunable *result-shaping* knob in the recommender.

One source of truth for the Taste Model control panel: each knob's label, plain-English
explanation, range, step, and default live here, and every consumer reads its value through
`get_param(store, name)` (falling back to the default). This replaces magic numbers that were
scattered across recommend.py, so the page can render the knobs generically and reset them.

Lane and genre *weights* are NOT here - those are multiplicative weights stored in the
`rec_weights` table (axes `lane:*` / `genre:*`); see LANES below for their labels/help. This
module owns the scalar params (windows, ratios, counts, penalties), stored in the `settings`
table as `rec_param:<name>` keys.

Embedding internals (dim, method, item2vec hyperparams, seeds) are deliberately NOT exposed:
hand-setting them only degrades quality, so they stay managed by Auto-tune.
"""
from dataclasses import dataclass
from yt_playlist.core.recommendation_settings import LANES, LANE_NAMES, MAX_TRACK_MINUTES

SETTING_PREFIX = "rec_param:"


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    group: str          # section the knob renders under ("discovery")
    explanation: str    # one-line, user-facing
    min: float
    max: float
    step: float
    default: float
    integer: bool = False
    advanced: bool = False
    boolean: bool = False
    low: str = ""
    high: str = ""
    percentage: bool = False


# Each default equals the constant it replaced, so behaviour is unchanged until a user moves it.
PARAMS = [
    ParamSpec('album_collab_weight', 'Album fit: playlist patterns', 'discovery',
              'Share of album affinity supplied by the track embedding when both playlist patterns '
              'and content features are available. Releases without one source use the available source.',
              0, 1, .05, .5, percentage=True),
    ParamSpec("album_menu_novelty", "Album menus: comfort and novelty", "discovery",
              "Comfort favors familiar releases and close model fits. Novelty reserves more places for "
              "unsaved releases and reaches further within the model-ranked frontier. Half requests two "
              "unsaved releases; unsaved does not mean unheard. Genres are chosen explicitly in the "
              "Albums tuner. Temporary adjustments last four hours without changing lasting taste.",
              0, 1, .05, .5, low="Comfort", high="Novelty", percentage=True),
    ParamSpec("bandit_reward_choice", "Mix response: deliberate choice", "discovery",
              "Positive support for the selected mix and relative choice evidence for its visible alternatives. "
              "Capped per group and UTC day; unchosen music is not a general dislike.", 0, 1, .05, .25),
    ParamSpec("bandit_reward_open", "Mix response: opening details", "discovery",
              "Weak positive allocation support for an observed opening. Missing opens supply no failure. "
              "Shares the same group-day cap as other mix responses.", 0, 1, .05, .05),
    ParamSpec("bandit_reward_start", "Mix response: confirmed start", "discovery",
              "Additional support after exact-context playback is confirmed. Successful creation alone "
              "does not supply this support. Shared response mass is capped per group and UTC day.", 0, 1, .05, .25),
    ParamSpec("bandit_reward_completion", "Mix response: observed track completion", "discovery",
              "Additional support when an attributed playhead reaches the existing completion rule. "
              "One or many completions share a group-day cap; completion does not prove enjoyment.", 0, 1, .05, .5),
    ParamSpec("bandit_reward_promotion", "Mix response: explicit adoption", "discovery",
              "Support for explicitly adopting a generated playlist into the collection. Saving a generated "
              "copy alone is not adoption. Shared response mass is capped per group and UTC day.", 0, 1, .05, 1),
    ParamSpec("bandit_reward_early_exit", "Mix response: observed early exit", "discovery",
              "Contextual counterevidence for an attributed early track exit under the existing skip rule. "
              "Closing a tab or a missing ending supplies no counterevidence; no artist or genre dislike is inferred.", 0, 1, .05, .25),
    ParamSpec("bandit_window_d", "Mix choice evidence window (days)", "discovery",
              "Mix menu allocation uses offers and successful creations within this wall-clock window. "
              "Older evidence expires; this does not remove saved musical interests.", 1, 365, 1, 90, integer=True),
    ParamSpec("bandit_exploration", "Mix menu exploration share", "discovery",
              "Share of new Home mix menus allocated uniformly across the eligible prepared menus. "
              "The remaining share follows windowed choice support; each whole-menu probability is recorded.",
              0, 1, 0.05, 0.15),
    ParamSpec("bandit_size_power", "Mix cluster size prior", "discovery",
              "Standing Home menu allocation multiplier: cluster size raised to this power. "
              "Zero removes the popularity prior; one gives proportional cluster-size influence.", 0, 1, 0.1, 0),
    ParamSpec("choice_recent_w", "Recent listening choices", "discovery",
              "Shared interest support from deliberate album, mix and On air choices. "
              "Episode influence is capped; passive generated tracks add no separate support.",
              0, 2, .05, .3),
    ParamSpec("choice_retention_d", "Unanswered choice retention (days)", "discovery",
              "Daily maintenance retires older menus with no choice, listening, save or feedback. "
              "Chosen menus and corrections remain within the storage budget. Reports cover retained evidence.",
              7, 730, 1, 180, integer=True),
    ParamSpec("choice_halflife_d", "Choice memory half-life (days)", "discovery",
              "Recent choice influence halves after this many days of wall time. NOW and Session "
              "use their existing hour-scale controls; saved support requires separate return days.",
              1, 90, 1, 7, integer=True),
    ParamSpec("choice_saved_days", "Choice days for saved support", "discovery",
              "Distinct UTC choice days before an interest earns lasting support shared across formats. "
              "Radio branches within one episode are correlated and capped.",
              2, 30, 1, 3, integer=True),
    ParamSpec("choice_saved_w", "Saved choice support", "discovery",
              "Standing support after repeated choice days clear the threshold. Removing source "
              "choices removes their contribution when the worker compiles evidence again.",
              0, 2, .05, .5),
    ParamSpec("choice_attribution_h", "Playback observation window (hours)", "discovery",
              "Maximum time after an album choice to link its exact queue, and after a confirmed "
              "context start to link a raw track exit. Applies across formats; missing exits remain unknown. "
              "Navigation alone is never confirmed listening.",
              1, 48, 1, 6, integer=True),
    ParamSpec("album_saved_weight", "Saved album interest", "discovery",
              "Standing support per saved record in shared taste contexts, in both scoring spaces. "
              "Albums share recommendation slots with curated playlists. Zero removes saved-only support.",
              0, 3, 0.1, 1),
    ParamSpec("album_feedback_weight", "More like this album", "discovery",
              "Standing multiplier for a saved or repeatedly heard album marked More like this. "
              "It changes that record's shared context weight; clearing album feedback removes it. "
              "Individual track ratings remain separate.", 1, 3, .1, 1.5),
    ParamSpec("album_return_weight", "Album return days", "discovery",
              "Standing support from tracks heard on multiple days, scaled by their share of the complete "
              "album tracklist. One repeated favorite gives a small boost; returning to most or all tracks "
              "gives more. Growth across days is logarithmic; same-day replays add no return support. "
              "Unknown tracklists supply no coverage boost. Shared across listening formats.",
              0, 3, 0.1, 1),
    ParamSpec("max_track_minutes", "Maximum track length (minutes)", "discovery",
              "Longest track allowed in generated song playlists. Lower limits keep DJ mixes and "
              "concerts out. Tracks with unknown length remain eligible.",
              MAX_TRACK_MINUTES.minimum, MAX_TRACK_MINUTES.maximum, 1, MAX_TRACK_MINUTES.default, integer=True),
    ParamSpec("comfort_min_plays", "Comfort Listening: minimum plays", "discovery",
              "Minimum past plays for a forgotten favorite to appear in Comfort Listening. "
              "Raise this to include only tracks you played often.",
              1, 50, 1, 4, integer=True),
    ParamSpec("comfort_recency_full_days", "Comfort Listening: wait before full priority (days)", "discovery",
              "How long a favorite needs to go unplayed to get full priority. Higher favors songs "
              "you have not heard in longer; lower brings recently played favorites back sooner.",
              1, 365, 1, 30, integer=True),
    ParamSpec("erosion_view_cap", "Home visits before suggestions refresh", "discovery",
              "Number of Home visits before cards show a new set of suggestions. Lower refreshes them sooner.",
              1, 10, 1, 3, integer=True),
    ParamSpec("generated_gc_days", "Generated playlist cleanup (days)", "discovery",
              "Generated playlists with fewer than half their tracks played are deleted after this "
              "many days, both locally and on YouTube.",
              1, 365, 1, 7, integer=True),
    ParamSpec("discovery_artist_limit", "Discovery: artists scanned", "discovery",
              "Number of your most-listened-to artists checked for albums to recommend. "
              "Lower keeps discovery closer to your current favorites.",
              10, 1000, 10, 100, integer=True, advanced=True),
    ParamSpec("discovery_albums_per_artist", "Discovery: albums per artist", "discovery",
              "Maximum albums from one artist available for discovery at once. "
              "The selection rotates through their catalog, including older releases.",
              1, 10, 1, 3, integer=True, advanced=True),
    ParamSpec("discovery_gc_days", "Unsaved discovery cleanup (days)", "discovery",
              "How long a discovered album, artist, or track stays in the pool after it is first shown "
              "to you. Tracks not yet shown expire this many days after being found. "
              "If you have not added it by then, it is removed. A track held in a generated "
              "playlist stays until that playlist is cleaned up.",
              1, 365, 1, 30, integer=True),
    ParamSpec("candidate_pool_factor", "Suggestions prepared per slot", "discovery",
              "Tracks prepared for each displayed recommendation. Higher gives rotating suggestions "
              "more variety and requires more processing.",
              2, 10, 1, 4, integer=True, advanced=True),
    ParamSpec("dislike_suppress_days", "Dislike ban length (days)", "discovery",
              "How long a thumbs-down hides a track before it can resurface.",
              1, 3650, 1, 365, integer=True),
    ParamSpec("cluster_content_weight", "Clusters: musical similarity", "discovery",
              "Balance musical similarity against shared playlist membership on the Clusters canvas. "
              "0 uses only playlists; higher gives genre and era more influence.",
              0.0, 1.0, 0.05, 0.30),
    # Cluster-ring shaping (defaults mirror embed.CLUSTER_BETA / embed.SEED_FANOUT; both apply at query
    # time, so a change takes effect on the next grow with no rebuild).
    ParamSpec("cluster_beta", "Clusters: effect of removing a track", "discovery",
              "How much removing a track reduces similar suggestions on that branch. "
              "0 only removes the track; higher also moves future suggestions away from its sound.",
              0.0, 2.0, 0.05, 0.60),
    ParamSpec("cluster_seed_spread", "Clusters: variety across starting tracks", "discovery",
              "How much each starting track shapes new suggestions. 0 uses their average sound; "
              "higher also finds music similar to each track individually.",
              0.0, 1.0, 0.05, 0.50, advanced=True),
    # --- Artist relationship model (#28). How its blocks combine when relating artists. content_weight
    # is the §B (genre/era/audio) fraction vs the §A co-curation block (collab = 1 - content, the
    # cluster_content_weight convention); edge_weight adds the §C Last.fm-edge bonus on top. ---
    ParamSpec("artist_content_weight", "Related artists: musical similarity", "discovery",
              "Balance genre, era, and audio similarity against shared playlist membership. "
              "0 uses only playlists; higher can also connect artists you have not put together.",
              0.0, 1.0, 0.05, 0.30, advanced=True),
    ParamSpec("artist_edge_weight", "Related artists: Last.fm influence", "discovery",
              "How much Last.fm's similar-artist links contribute alongside playlist and musical similarity. "
              "This can connect artists outside your library.",
              0.0, 1.0, 0.05, 0.10, advanced=True),
    # --- Breadth steering (#7). The interactive Breadth bar on the Home fingerprint binds to
    # breadth_bias; breadth_gain is its (advanced) sensitivity. Default 0 == today's behaviour. ---
    ParamSpec("breadth_bias", "Genre variety", "discovery",
              "Give the genres you play less often more chances, or stick to your usual favorites. "
              "The middle keeps your usual balance. This draws from genres already in your library.",
              -1.0, 1.0, 0.05, 0.0, low="Usual favorites", high="Less-played genres"),
    ParamSpec("breadth_gain", "Genre variety sensitivity", "discovery",
              "How strongly the Genre variety slider changes recommendations. Higher makes each adjustment stronger.",
              0.2, 3.0, 0.05, 1.0, advanced=True),
    # --- Right-now responsiveness (transient model). Defaults == the constants below. ---
    ParamSpec("play_transient_w", "Follow recent plays", "transient",
              "Adds extra preference for music like your recent plays. Off removes this boost; "
              "your listening history still counts. Stronger settings can let one session take over.",
              0.0, 2.0, 0.05, 0.30, low="0% · Off", high="100% · Strongest", percentage=True),
    ParamSpec("like_transient_w", "Follow recent likes", "transient",
              "Adds extra preference for music like your recent likes. Off removes this boost "
              "without undoing likes. Stronger settings can crowd out other music you enjoy.",
              0.0, 2.0, 0.05, 0.45, low="0% · Off", high="100% · Strongest", percentage=True),
    ParamSpec("dislike_transient_w", "Recent dislikes: influence", "transient",
              "How much recent dislikes reduce recommendations for similar music.",
              0.0, 3.0, 0.05, 1.50),
    ParamSpec("dislike_durable_w", "Remember disliked music", "discovery",
              "Keep reducing matches to disliked tracks after recent feedback fades. "
              "Removing a dislike removes this influence; genre and era preferences stay independent.",
              0.0, 1.0, 0.05, 0.35),
    ParamSpec("audio_transient_w", "Follow recent sound", "transient",
              "Adds extra preference for similar tempo, energy, and mood. Off removes this boost. "
              "Stronger settings can make picks sound alike. Requires sound information.",
              0.0, 2.0, 0.05, 0.30, low="0% · Off", high="100% · Strongest", percentage=True),
    ParamSpec("axis_weight_cap", "Preference strength", "discovery",
              "Higher lets your genre, era, and artist preferences override how well a track otherwise "
              "matches your taste. Too high can promote weaker matches just because their tags fit. "
              "Excluded genres stay excluded at every setting.",
              1.0, 16.0, 0.1, 1.4, advanced=True),
    ParamSpec("facet_gain", "Recent genre, era, and artist influence", "transient",
              "How strongly recent genre, era, and artist preferences change recommendation rankings.", 0.0, 1.0, 0.05, 0.35),
    ParamSpec("play_halflife_d", "Play memory half-life (days)", "transient",
              "Days until a play has half its original influence. Lower makes recent plays fade sooner.",
              0.5, 30, 0.5, 3),
    ParamSpec("mood_halflife_d", "Mood memory half-life (days)", "transient",
              "Days until a mood choice has half its original influence. Lower makes mood choices fade sooner.", 1, 60, 1, 7, integer=True),
    ParamSpec("like_halflife_d", "Like memory half-life (days)", "transient",
              "Days until a like has half its original influence on current recommendations.", 1, 90, 1, 21, integer=True),
    ParamSpec("dislike_halflife_d", "Dislike fade half-life (days)", "transient",
              "Days until a dislike has half its original effect on recent genre and era preferences. "
              "Remembered disliked music and the track exclusion have separate controls.",
              5, 180, 5, 5, integer=True),
    ParamSpec("mood_alpha", "Mood choice influence", "transient",
              "How strongly a mood choice changes recommendations relative to your saved taste.",
              0.0, 1.0, 0.05, 0.35),
    ParamSpec("weight_revert_halflife_d", "Learned adjustment fade time (days)", "transient",
              "Days for saved genre and recommendation-mix weights to move halfway back to neutral (1). "
              "Applies to learned and manual changes. Genre weights set to 0 stay muted until changed. "
              "Higher keeps adjustments longer.", 7, 365, 7, 60, integer=True, advanced=True),
    ParamSpec("recent_play_limit", "Recent plays and likes to consider", "transient",
              "Number of recent plays and likes used to adjust current recommendations.", 5, 200, 5, 50, integer=True),
    ParamSpec("facet_mult_min", "Minimum recent-preference multiplier", "transient",
              "Lowest score multiplier recent negative feedback can apply to a genre, era, or artist.",
              0.0, 1.0, 0.05, 0.35, advanced=True),
    ParamSpec("facet_mult_max", "Maximum recent-preference multiplier", "transient",
              "Highest score multiplier recent positive feedback can apply to a genre, era, or artist.", 1.0, 4.0, 0.1, 2.5, advanced=True),
    # --- NOW/session layers (#88 layered taste model): a confidence-gated posterior over taste modes
    # read from the last few hours of real plays (NOW), plus a slower same-sitting carry-over
    # (session). Defaults tuned so a normal listening session clears the NOW gate within a couple of
    # tracks; a quiet stretch (or one lone play) returns no read rather than guessing. ---
    ParamSpec("now_window_h", "Current listening window (hours)", "transient",
              "Hours of recent plays used to choose your current listening style. "
              "Lower reacts faster; higher includes more of the session.",
              1, 24, 1, 6, integer=True),
    ParamSpec("now_min_events", "Current listening: minimum plays", "transient",
              "Distinct recent plays with enough track information required before choosing a current listening style.",
              2, 10, 1, 3, integer=True, advanced=True),
    ParamSpec("now_gain", "Current listening: influence", "transient",
              "How strongly your current listening style affects Home and Radio once enough plays are available. "
              "Radio's stream controls and explicit stream choices take precedence.",
              0.0, 2.0, 0.05, 0.6),
    ParamSpec("session_halflife_h", "Session memory half-life (hours)", "transient",
              "Hours until this session's influence halves. Higher keeps it active across longer breaks.", 0.5, 24, 0.5, 4),
    ParamSpec("session_alpha", "Session influence", "transient",
              "How strongly this listening session affects recommendations relative to your saved taste.",
              0.0, 1.0, 0.05, 0.25),
    # --- Skips (#84 read-time classification of track_exit/bye rows). Defaults mirror the
    # play/like/dislike transient channels: a moderate push with a two-week memory. ---
    ParamSpec("skip_transient_w", "Recent skips: influence", "transient",
              "How much recent skips reduce recommendations for similar music. 0 ignores skips.", 0.0, 2.0, 0.05, 0.6),
    ParamSpec("skip_halflife_d", "Skip memory half-life (days)", "transient",
              "Days until a skip has half its original influence on recommendations.",
              1, 60, 1, 14, integer=True),
    # --- Learning (graduation: right-now -> permanent). Defaults == the constants below. ---
    ParamSpec("theme_threshold", "Learning threshold", "graduation",
              "Amount of repeated activity needed to change a saved genre, era, or artist preference. "
              "Higher requires more evidence and learns more slowly.", 0.2, 5.0, 0.1, 1.2),
    ParamSpec("graduate_up", "Learned preference increase", "graduation",
              "Multiplier applied to a saved weight after enough positive evidence. Higher makes each increase larger.",
              1.0, 1.5, 0.01, 1.05, advanced=True),
    ParamSpec("graduate_down", "Learned preference decrease", "graduation",
              "Multiplier applied to a saved weight after enough negative evidence. Lower makes each decrease larger.",
              0.5, 1.0, 0.01, 0.95, advanced=True),
    ParamSpec("source_w_like", "Speed: likes (in the player)", "graduation",
              "How quickly likes made while listening update saved preferences. "
              "These likes also affect current recommendations immediately.", 0.0, 2.0, 0.05, 1.0, advanced=True),
    ParamSpec("source_w_like_synced", "Speed: likes (found by sync)", "graduation",
              "How quickly likes found during sync update saved preferences. Their original dates "
              "are unknown, so they do not affect current mood. At the default, about three similar "
              "likes produce one saved preference update.",
              0.0, 2.0, 0.05, 0.4, advanced=True),
    ParamSpec("source_w_vibe", "Speed: mood choices", "graduation",
              "How quickly mood choices update saved preferences.", 0.0, 2.0, 0.05, 1.0, advanced=True),
    ParamSpec("source_w_feedback", "Speed: suggestion feedback", "graduation",
              "How quickly dismissed suggestions and Home feedback choices update saved preferences.", 0.0, 2.0, 0.05, 1.0, advanced=True),
    ParamSpec("pop_mainstream_min", "Mainstream threshold", "discovery",
              "Minimum Deezer popularity score for a track to count as mainstream when you choose "
              "'too mainstream' feedback. Tracks without a popularity score are excluded from that category.",
              0, 1000000, 50000, 500000, integer=True, advanced=True),
    # The intent-vs-behavior graduation balance: two visible (non-advanced) knobs. Both channels now
    # graduate by the same daily-exposure mechanic, so the weights are directly comparable (per-day vs
    # per-day). The default ratio 0.5 : 0.08 (~6:1) encodes "explicit steering moves long-term taste
    # faster than passive listening" (spec §2).
    ParamSpec("source_w_slider", "Learning rate: slider choices", "graduation",
              "How much each day with an active Home slider adjustment changes saved preferences.",
              0.0, 2.0, 0.05, 0.5),
    ParamSpec("source_w_play", "Learning rate: listening", "graduation",
              "How much each day of repeated listening changes saved preferences. "
              "The default learns more slowly from plays than from deliberate slider choices.",
              0.0, 1.0, 0.01, 0.08),
    ParamSpec("modes_k", "Listening styles: starting groups", "discovery",
              "Number of track groups tested when finding listening styles. "
              "Higher finds more specific styles; lower keeps them broader.",
              4, 40, 1, 14, integer=True, advanced=True),
    ParamSpec("modes_min_members", "Listening styles: minimum tracks", "discovery",
              "Minimum tracks a group needs to count as a listening style. Smaller groups are discarded.",
              10, 200, 5, 40, integer=True, advanced=True),
    ParamSpec("modes_match_threshold", "Listening styles: matching threshold", "discovery",
              "Similarity required to recognize a listening style after rebuilding. "
              "Higher is more likely to treat a changed group as a new style.",
              0.0, 1.0, 0.05, 0.60, advanced=True),
    ParamSpec("modes_pool_limit", "Listening styles: tracks considered", "discovery",
              "Minimum ranked tracks requested when grouping recommendations by listening style. "
              "Library recommendations also consider every modeled track so smaller genres can fill a mix.",
              50, 2000, 50, 400, integer=True, advanced=True),
    ParamSpec("modes_cand_per_mode", "Listening styles: tracks prepared per card", "discovery",
              "Tracks saved for each listening-style card to rotate through before repeating.",
              12, 200, 4, 40, integer=True, advanced=True),
    ParamSpec("modes_menu_size", "Listening styles: Home cards", "discovery",
              "Number of different listening-style cards shown on Home at once.",
              2, 6, 1, 4, integer=True, advanced=True),
    ParamSpec("fresh_artist_cap", "Fresh songs: max per artist", "discovery",
              "Maximum tracks from one artist on the Fresh card. Lower includes a wider range of artists.",
              1, 12, 1, 2, integer=True, advanced=True),
    ParamSpec("modes_artist_cap", "Listening styles: tracks per artist", "discovery",
              "Maximum tracks from one artist on a listening-style card.",
              1, 12, 1, 2, integer=True, advanced=True),
    ParamSpec("modes_album_cap", "Listening styles: tracks per album", "discovery",
              "Maximum tracks from one album on a listening-style card. Tracks without an album are not limited.",
              1, 12, 1, 2, integer=True, advanced=True),
    ParamSpec("comfort_min_pool", "Comfort Listening: minimum suggestions", "discovery",
              "Minimum forgotten favorites needed to show Comfort Listening. "
              "Until then, Throwback, Time Flies, or Recent Picks fills the fourth card.",
              0, 500, 4, 36, integer=True, advanced=True),
    ParamSpec("onboard_feedback_min", "Getting started: feedback needed", "discovery",
              "Number of likes and dislikes that ends the getting-started suggestions. "
              "Personal mixes appear once they are ready.",
              1, 200, 1, 10, integer=True, advanced=True),
    # --- Per-mode Personalized PageRank ranker (#57). Experimental A/B: PPR walk vs the current
    # in-mode ordering. All advanced (a tuner sweeps them; hand-setting only degrades quality). ---
    ParamSpec("ppr_alpha", "Experimental ranking: exploration distance", "discovery",
              "How far the experimental ranking follows shared-listening connections before restarting "
              "from a style's starting tracks. Higher explores farther.",
              0.5, 0.99, 0.01, 0.85, advanced=True),
    ParamSpec("ppr_iters", "Experimental ranking: calculation limit", "discovery",
              "Maximum calculation rounds for experimental ranking. It stops sooner if rankings stabilize.",
              10, 200, 5, 50, integer=True, advanced=True),
    ParamSpec("ppr_tol", "Experimental ranking: calculation precision", "discovery",
              "How small a change counts as finished. Lower calculates more precisely but can take "
              "longer. The default is enough for normal use.",
              0.0, 0.01, 0.000001, 0.000001, advanced=True),
    ParamSpec("ppr_rank_depth", "Experimental ranking: tracks saved", "discovery",
              "Top-ranked tracks saved for each listening-style card by experimental ranking.",
              50, 2000, 50, 200, integer=True, advanced=True),
    ParamSpec("ppr_ab_share", "Experimental ranking: share of cards", "discovery",
              "Share of listening-style cards using experimental ranking. 0 uses standard ranking "
              "for all cards with shadow replay; 1 retains experimental ranking for all. The default "
              "0.5 is a live experiment. Recorded retirement disables shadow replay at 0.",
              0.0, 1.0, 0.05, 0.5, advanced=True),
]

PARAMS_BY_NAME = {p.name: p for p in PARAMS}

# #114: the everyday controls. Other discovery knobs are grouped into visible feature sections.
SEMANTIC_PARAMS = ("breadth_bias", "play_transient_w", "like_transient_w", "audio_transient_w")


# Lane weights (rec_weights table, axis `lane:<name>`): labels + help for the page. Clamp [0.2, 3.0].
LANE_MIN, LANE_MAX, LANE_DEFAULT = 0.2, 3.0, 1.0

# Genre-family weights (rec_weights table, axis `genre:<family>`): 0 mutes, 1 neutral, 2 favors.
GENRE_MIN, GENRE_MAX, GENRE_DEFAULT, GENRE_STEP = 0.0, 2.0, 1.0, 0.1

# #18: how much of the transient facet lean reaches OUTWARD discovery (new artists/albums). Damped
# (<1) vs the in-library surfaces, so the deliberately-stable discovery pool isn't yanked around by a
# single mood gesture; permanent genre weights still apply at full strength (and 0 hard-excludes).
DISCOVERY_TRANSIENT_DAMP = 0.5


def _clamp(spec, value):
    if spec.boolean:
        return value.lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
    v = max(spec.min, min(spec.max, float(value)))
    return int(round(v)) if spec.integer else v


def get_param(store, name):
    """Current value of a scalar knob - the stored override, clamped, or the registry default."""
    return setting_param(store.settings, name)


def setting_param(settings, name):
    """Read a registered parameter through its explicit settings repository."""
    spec = PARAMS_BY_NAME[name]
    raw = settings.get_setting(SETTING_PREFIX + name)
    if raw is None or raw == "":
        return spec.default
    if spec.boolean:
        return raw == "1"
    try:
        return _clamp(spec, raw)
    except (TypeError, ValueError):
        return spec.default


def max_track_duration_s(store):
    """Shared track-length limit for recommendation pools and generated playlist saves (#44)."""
    return 60 * get_param(store, "max_track_minutes")


def set_param(store, name, value) -> None:
    """Persist a scalar knob override (clamped to its spec range)."""
    spec = PARAMS_BY_NAME[name]
    if spec.boolean:
        store.settings.set_setting(SETTING_PREFIX + name, "1" if _clamp(spec, value) else "0")
    else:
        store.settings.set_setting(SETTING_PREFIX + name, str(_clamp(spec, value)))


def reset_param(store, name) -> None:
    """Drop a scalar knob's override so it reverts to the registry default."""
    PARAMS_BY_NAME[name]   # validate the name
    store.settings.delete_setting(SETTING_PREFIX + name)


def reset_all_params(store) -> None:
    """Revert every scalar knob to its default."""
    for p in PARAMS:
        store.settings.delete_setting(SETTING_PREFIX + p.name)


# --- Transient model (lifecycle / recency) ---
SYNC_STALE_S = 24 * 3600       # sync older than this is "stale" (also used by actions.sync_status)
# Source weights into the transient leans
PLAY_TRANSIENT_W = 0.30        # one recent play's positive push
LIKE_TRANSIENT_W = 0.45        # one recent like's positive push to facet leans (stronger than a play)
DISLIKE_TRANSIENT_W = 1.50     # one recent dislike's negative push (strong, explicit)
RECENT_PLAY_LIMIT = 50         # how many recent plays feed the transient leans
# Facet overlay shape (read by _axis_weights_for and roll_recipe). The transient overlay DE-RANKS but
# never banishes: one gentle "less X" roughly halves X in the feed, "a lot"/sustained strongly reduces
# but always leaves some present. Banishing a facet is the job of dislike (a ban) or graduation
# (sustained transient → a lasting permanent-weight nudge), not a single transient gesture.
FACET_GAIN = 0.35              # slope: how hard a unit lean bends the facet multiplier off 1.0
FACET_MULT_MIN = 0.35          # floor: even the strongest transient lean keeps a facet present, not muted
FACET_MULT_MAX = 2.5           # ceiling: cap on how much a positive lean can boost a facet
# Dislike (permanent suppression side)
DISLIKE_SUPPRESS_DAYS = 365
# Graduation
THEME_THRESHOLD = 1.2
GRADUATE_UP = 1.05
GRADUATE_DOWN = 0.95
# --- Graduation source weights (Approach 1: source-aware funnel) ---
# Each transient signal contributes a source-weighted amount to rec_theme. The transient effect is
# immediate; graduation is gated by accumulation past THEME_THRESHOLD. Calibrated so today's vibe
# lever is preserved (a mood event still contributes ±1 with SOURCE_W_VIBE=1.0).
SOURCE_W_VIBE = 1.0       # vibe / facet lever (unchanged from today)
SOURCE_W_LIKE = 1.0       # thumbs-up observed live in the player - strong (real action time)
SOURCE_W_LIKE_SYNCED = 0.4  # like discovered by the library sync - low (no reliable action time;
                            # a single one must not cross THEME_THRESHOLD on its own)
SOURCE_W_SLIDER = 0.5     # per held-day of a full lean (~2-3 held days -> one graduation step)
SOURCE_W_PLAY = 0.08      # one play - weak; passive listening must not silently rewrite taste
PLAY_GRAD_SESSION_CAP = 0.4   # max play-graduation contribution per session (a binge can't graduate)
