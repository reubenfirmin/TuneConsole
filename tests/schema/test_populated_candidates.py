"""Complete populated ranks and actual serving behavior across the candidate cutover."""
import hashlib
import json
import time

from tests.schema.test_populated_groups import populated, storage
from yt_playlist.core import json_cache, recommendation_outputs
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.migrations import Migration, migrate, registry
from yt_playlist.core.migrations import v070_policy_documents as policies
from yt_playlist.core.migrations import v071_album_descriptions as descriptions
from yt_playlist.core.migrations import v072_compact_groups as groups
from yt_playlist.core.migrations import v073_recommendation_publications as candidates
from yt_playlist.core.store import Store
from yt_playlist.rec import home_vibe, mode_surfaces
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.repos import proposal_cache
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.recommendation_publications import RecommendationPublications


def predecessor_registry():
    return (*registry.MIGRATIONS[:69], Migration(70, 'policy_documents', policies.apply),
            Migration(71, 'album_description_owners', descriptions.apply),
            Migration(72, 'compact_groups', groups.apply, rebuild_tables=True))


def ranked(values, *, bands=None, scores=None):
    hashed, count = hashlib.sha256(), 0
    for rank, item in enumerate(values):
        key = item['key'] if isinstance(item, dict) else item
        score = item.get('score') if isinstance(item, dict) else (scores or {}).get(key)
        band, genre = (bands or {}).get(key, (None, None))
        hashed.update(canonical([key, rank, float(score) if score is not None else None, band, genre]))
        count += 1
    return count, hashed.hexdigest()


def expected_pools(bundle, fresh):
    result = {}
    for mode, lanes in bundle.items():
        if not mode.startswith('_'):
            for lane, values in lanes.items():
                result['home', mode, lane] = ranked(values)
    if fresh is not None:
        result['fresh', 'all', 'fresh'] = ranked(fresh)
    radio = bundle.get('_radio', {})
    scores = radio.get('scores', {})
    for mode, values in radio.get('members', {}).items():
        bands = {key: (int(band), genre) for band, genres in
                 radio.get('intensity', {}).get('clusters', {}).get(mode, {}).items()
                 for genre, keys in genres.items() for key in keys}
        result['radio', mode, 'membership'] = ranked(values, bands=bands, scores=scores)
    if scores:
        result['radio', 'all', 'scores'] = ranked(sorted(scores), scores=scores)
    for mode, values in bundle.get('_ppr', {}).items():
        result['radio', mode, 'ppr'] = ranked(values)
    for preset, lanes in bundle.get('_vibes', {}).get('pools', {}).items():
        for lane, values in lanes.items():
            result['vibe:' + preset, '', lane] = ranked(values)
    for preset, lanes in bundle.get('_vibes', {}).get('genres', {}).items():
        for lane, genres in lanes.items():
            for genre, values in genres.items():
                result['vibe:' + preset, genre, lane] = ranked(values)
    return result


def stored_pool(conn, pool):
    cursor = conn.execute('SELECT r.identity_key,c.rank,c.score,b.intensity_band,b.genre_key '
        'FROM recommendation_candidates c JOIN recordings r ON r.id=c.recording_id '
        'LEFT JOIN recommendation_radio_bands b ON b.pool_id=c.pool_id AND b.recording_id=c.recording_id '
        'WHERE c.pool_id=? UNION ALL SELECT raw_key,rank,score,intensity_band,genre_key '
        'FROM unresolved_recommendation_candidates WHERE pool_id=? ORDER BY 2', (pool, pool))
    hashed, count = hashlib.sha256(), 0
    while rows := cursor.fetchmany(128):
        for key, rank, score, band, genre in rows:
            hashed.update(canonical([key, rank, float(score) if score is not None else None, band, genre]))
            count += 1
    return count, hashed.hexdigest()


def card_keys(cards):
    return [(card['lane'], card.get('mode_id'), card.get('ranker'),
             [track['key'] for track in card['tracks']]) for card in cards]


def radio_keys(store, at):
    radio = RadioCandidates(store, at, comparison=None)
    try:
        radio.rng.seed(0)
        return [track['key'] for track in radio.seed_offer(12)]
    finally:
        radio.close()


def test_all_populated_ranks_and_representative_serving_survive_cutover(populated, monkeypatch):
    _source, conn, output = populated
    migrate(conn, registry=predecessor_registry())
    raw = proposal_cache.read(conn, 'mode_bundles')
    bundle = json_cache.decode(raw, profile_fields=('digest',), max_bytes=128 * 1024 * 1024)
    fresh = json_cache.decode(proposal_cache.read(conn, 'fresh_pool'), profile_fields=('digest',),
                              max_bytes=128 * 1024 * 1024)
    expected = expected_pools(bundle, fresh)
    del bundle, fresh
    at = conn.execute("SELECT built_at FROM rec_proposal_cache_heads WHERE surface='mode_bundles'").fetchone()[0]
    before = storage(conn)
    store = Store(output / 'populated.db')
    try:
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        order = [mode['mode_id'] for mode in store.modes.list_modes(active_only=True)]
        # A fixed neutral context and mode order isolates storage behavior from
        # preference selection and stochastic mode rotation on the actual data.
        monkeypatch.setattr(home_vibe, 'current', lambda *args, **kwargs: None)
        old_home = card_keys(mode_surfaces.assemble_cards(store, at, 0, mode_order=order))
        old_radio = radio_keys(store, at)
        started = time.monotonic()
        migrate(store.conn, registry=(*predecessor_registry(),
            Migration(73, 'recommendation_publications', candidates.apply)))
        migration_seconds = time.monotonic() - started
        candidates.validate(store.conn)
        actual = {(surface, mode, purpose): stored_pool(store.conn, pool) for surface, mode, purpose, pool in
                  store.conn.execute('SELECT surface,mode_key,purpose,pool_id FROM recommendation_surface_pools')}
        assert actual == expected
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        original = RecommendationProposals._proposal_json
        def read(self, surface):
            assert surface not in ('mode_bundles', 'fresh_pool'), 'Foreground decoded the retired bundle'
            return original(self, surface)
        monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)
        original_page, pages = RecommendationPublications.candidate_page, []
        def page(self, *args, **kwargs):
            result = original_page(self, *args, **kwargs)
            pages.append((kwargs['limit'], len(result['tracks'])))
            return result
        monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
        started = time.monotonic()
        new_home = card_keys(mode_surfaces.assemble_cards(store, at, 0, mode_order=order))
        home_seconds = time.monotonic() - started
        started = time.monotonic()
        new_radio = radio_keys(store, at)
        radio_seconds = time.monotonic() - started
        report = {'slots': len(expected), 'logical_candidates': sum(row[0] for row in expected.values()),
                  'migration_seconds': migration_seconds, 'home_seconds': home_seconds,
                  'radio_seconds': radio_seconds, 'old_home': old_home, 'new_home': new_home,
                  'old_radio': old_radio, 'new_radio': new_radio, 'candidate_pages': len(pages),
                  'max_page_limit': max(limit for limit, _ in pages),
                  'max_hydrated_page': max(size for _, size in pages),
                  'before_bytes': before, 'after_bytes': storage(store.conn),
                  'output_file_bytes': sum(p.stat().st_size for p in recommendation_outputs.directory(store.conn).rglob('*')
                                           if p.is_file())}
        (output / 'candidate-cutover.json').write_text(json.dumps(report, indent=2) + '\n')
        assert pages and all(limit <= 128 and count <= limit for limit, count in pages)
        assert old_home and old_home == new_home
        assert old_radio and old_radio == new_radio
    finally:
        store.conn.close()
