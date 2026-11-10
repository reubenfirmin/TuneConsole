"""Current row constraints shared by audits and future-write admission."""


def integer(column, minimum=0):
    return f"typeof({column})='integer' AND {column}>={minimum}"


def flag(column, *, nullable=False):
    expression = f"typeof({column})='integer' AND {column} IN (0,1)"
    return f'{column} IS NULL OR ({expression})' if nullable else expression


def finite(column, *, nullable=True):
    expression = f"typeof({column}) IN ('integer','real') AND abs({column})<=1.7976931348623157e308"
    return f'{column} IS NULL OR ({expression})' if nullable else expression


CONTRACTS = {
    'recordings': {'provisional':flag('provisional')},
    'taste_modes': {'active':flag('active'),'size':integer('size'),
                   'first_seen':finite('first_seen',nullable=False),'last_seen':finite('last_seen',nullable=False)},
    'youtube_items': {'available':flag('available',nullable=True)},
    'unresolved_items': {'available':flag('available',nullable=True)},
    'playlists': {'track_count':'track_count IS NULL OR ('+integer('track_count')+')'},
    'entry_artists': {'position':integer('position')},
    'album_artists': {'position':integer('position')},
    'discovery_memberships': {'offered_count':integer('offered_count'),
        **{column:finite(column) for column in ('found_at','first_shown','last_shown')}},
    'album_discovery_memberships': {
        **{column:integer(column) for column in ('offered_count','legacy_offered_count')},
        **{column:finite(column) for column in ('found_at','first_shown','last_shown','legacy_first_shown','legacy_last_shown')}},
    'discovered_artists': {
        **{column:integer(column) for column in ('offered_count','legacy_offered_count')},
        **{column:finite(column) for column in ('score','found_at','first_shown','last_shown','legacy_first_shown','legacy_last_shown')}},
    'discovery_legacy_offers': {'offered_count':integer('offered_count'),
        **{column:finite(column) for column in ('first_shown','last_shown')}},
    'discovery_impressions': {'shown_at':finite('shown_at',nullable=False)},
    'unresolved_discovery_impressions': {'shown_at':finite('shown_at',nullable=False)},
    'rec_response_events': {'is_generated':flag('is_generated')},
    'rec_served_impressions': {'rank':integer('rank'),'score':finite('score'),
                              'served_at':finite('served_at',nullable=False)},
    'rec_content_pending': {'position':integer('position'),'revision':integer('revision',1)},
    'rec_musical_pending': {'position':integer('position'),'revision':integer('revision',1),'cursor':integer('cursor')},
    'rec_vibe_pending': {'position':integer('position'),'revision':integer('revision',1)},
}
