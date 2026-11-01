"""Relocate complete prepared candidate pools into immutable typed publications."""
import hashlib
from dataclasses import replace

from .runner import MigrationError


def _finite(column, *, nullable=False):
    condition = f"typeof({column}) IN ('real','integer') AND abs({column})<=1.7976931348623157e308"
    return f'CHECK({column} IS NULL OR ({condition}))' if nullable else f'CHECK({condition})'


def _digest(column):
    return f"CHECK(length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*')"


def statements():
    yield ('CREATE TABLE recommendation_generations (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'external_key TEXT NOT NULL UNIQUE CHECK(length(external_key)>0),'
           'policy_version TEXT NOT NULL REFERENCES recommendation_policies(version),'
           'group_generation_id INTEGER REFERENCES group_publications(generation_id),'
           'output_digest TEXT NOT NULL ' + _digest('output_digest') + ','
           "bundle_required INTEGER NOT NULL DEFAULT 0 CHECK(typeof(bundle_required)='integer' AND bundle_required IN (0,1)),"
           "input_revision INTEGER CHECK(input_revision IS NULL OR (typeof(input_revision)='integer' AND input_revision>=0)),"
           "revision_reason TEXT CHECK(revision_reason='legacy_not_recorded'),"
           "status TEXT NOT NULL DEFAULT 'building' CHECK(status IN ('building','ready','published','retiring')),"
           'created_at REAL NOT NULL ' + _finite('created_at') + ',published_at REAL '
           + _finite('published_at', nullable=True) + ','
           'CHECK((input_revision IS NOT NULL AND revision_reason IS NULL) OR '
           '(input_revision IS NULL AND revision_reason IS NOT NULL)),'
           "CHECK((status IN ('building','ready') AND published_at IS NULL) OR status='retiring' OR "
           "(status='published' AND published_at IS NOT NULL)))")
    yield 'CREATE INDEX ix_recommendation_generation_policy ON recommendation_generations(policy_version,id)'
    yield ('CREATE INDEX ix_recommendation_generation_group ON recommendation_generations(group_generation_id,id) '
           'WHERE group_generation_id IS NOT NULL')
    yield ('CREATE TABLE recommendation_builds (generation_id INTEGER PRIMARY KEY NOT NULL '
           'REFERENCES recommendation_generations(id),owner TEXT NOT NULL CHECK(length(owner)>0),'
           'expires_at REAL NOT NULL ' + _finite('expires_at') + ',plan_digest TEXT NOT NULL '
           + _digest('plan_digest') + ') WITHOUT ROWID')
    yield 'CREATE INDEX ix_recommendation_build_expiry ON recommendation_builds(expires_at,generation_id)'
    yield ('CREATE TABLE recommendation_generation_surfaces (generation_id INTEGER NOT NULL '
           'REFERENCES recommendation_generations(id),surface TEXT NOT NULL CHECK(length(surface)>0),'
           "pool_count INTEGER NOT NULL CHECK(typeof(pool_count)='integer' AND pool_count>=0),"
           "output_count INTEGER NOT NULL CHECK(typeof(output_count)='integer' AND output_count>=0),"
           'PRIMARY KEY(generation_id,surface)) WITHOUT ROWID')
    yield ('CREATE TABLE recommendation_bundle_framing (generation_id INTEGER PRIMARY KEY NOT NULL '
           'REFERENCES recommendation_generations(id),'
           "presence INTEGER NOT NULL CHECK(typeof(presence)='integer' AND presence BETWEEN 0 AND 2047),"
           "comfort_pool INTEGER CHECK(comfort_pool IS NULL OR (typeof(comfort_pool)='integer' AND comfort_pool>=0)),"
           "year_low INTEGER CHECK(year_low IS NULL OR typeof(year_low)='integer'),"
           "year_high INTEGER CHECK(year_high IS NULL OR typeof(year_high)='integer'),"
           "full_pools INTEGER CHECK(full_pools IS NULL OR (typeof(full_pools)='integer' AND full_pools IN (0,1))),"
           "unmodeled INTEGER CHECK(unmodeled IS NULL OR (typeof(unmodeled)='integer' AND unmodeled IN (0,1))),"
           'musical_generation TEXT,radio_generation TEXT,radio_space TEXT,'
           "intensity_version INTEGER CHECK(intensity_version IS NULL OR (typeof(intensity_version)='integer' AND intensity_version>=0)),"
           'radio_profile_version TEXT,vibe_version TEXT,vibe_profile_version TEXT,'
           'CHECK((year_low IS NULL AND year_high IS NULL) OR '
           '(year_low IS NOT NULL AND year_high IS NOT NULL AND year_low<=year_high))) WITHOUT ROWID')
    yield ('CREATE TABLE recommendation_heads (surface TEXT PRIMARY KEY NOT NULL CHECK(length(surface)>0),'
           'generation_id INTEGER NOT NULL,FOREIGN KEY(generation_id,surface) '
           'REFERENCES recommendation_generation_surfaces(generation_id,surface)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_recommendation_head_generation ON recommendation_heads(generation_id,surface)'
    yield ('CREATE TABLE recommendation_pools (id INTEGER PRIMARY KEY AUTOINCREMENT,'
           'pool_key TEXT NOT NULL CHECK(length(pool_key)>0),'
           'policy_version TEXT NOT NULL REFERENCES recommendation_policies(version),'
           'group_version_id INTEGER REFERENCES group_versions(id),'
           'input_fingerprint TEXT NOT NULL ' + _digest('input_fingerprint') + ','
           'content_digest TEXT NOT NULL UNIQUE ' + _digest('content_digest') + ','
           "purpose TEXT NOT NULL CHECK(purpose IN ('card','radio','ppr','vibe','temporal','fresh')),"
           "eligible_count INTEGER NOT NULL CHECK(typeof(eligible_count)='integer' AND eligible_count>=0),"
           "complete INTEGER NOT NULL DEFAULT 0 CHECK(typeof(complete)='integer' AND complete IN (0,1)))")
    yield 'CREATE INDEX ix_recommendation_pool_policy ON recommendation_pools(policy_version,id)'
    yield ('CREATE INDEX ix_recommendation_pool_group ON recommendation_pools(group_version_id,id) '
           'WHERE group_version_id IS NOT NULL')
    yield ('CREATE TABLE recommendation_build_pools (generation_id INTEGER NOT NULL '
           'REFERENCES recommendation_builds(generation_id),pool_id INTEGER NOT NULL '
           'REFERENCES recommendation_pools(id),PRIMARY KEY(generation_id,pool_id)) WITHOUT ROWID')
    yield ('CREATE TABLE recommendation_pool_retirements (pool_id INTEGER PRIMARY KEY NOT NULL '
           'REFERENCES recommendation_pools(id)) WITHOUT ROWID')
    yield ('CREATE TABLE recommendation_candidates (pool_id INTEGER NOT NULL REFERENCES recommendation_pools(id),'
           'recording_id INTEGER NOT NULL REFERENCES recordings(id),'
           "rank INTEGER NOT NULL CHECK(typeof(rank)='integer' AND rank>=0),score REAL "
           + _finite('score', nullable=True) + ',PRIMARY KEY(pool_id,recording_id),UNIQUE(pool_id,rank)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_recommendation_candidate_recording ON recommendation_candidates(recording_id,pool_id)'
    yield ('CREATE TABLE unresolved_recommendation_candidates (pool_id INTEGER NOT NULL '
           'REFERENCES recommendation_pools(id),raw_key TEXT NOT NULL CHECK(length(raw_key)>0),'
           "rank INTEGER NOT NULL CHECK(typeof(rank)='integer' AND rank>=0),score REAL "
           + _finite('score', nullable=True) + ',intensity_band INTEGER '
           "CHECK(intensity_band IS NULL OR (typeof(intensity_band)='integer' AND intensity_band BETWEEN 0 AND 50)),"
           "genre_key TEXT,reason TEXT NOT NULL CHECK(reason='missing_recording'),"
           'CHECK((intensity_band IS NULL)=(genre_key IS NULL)),PRIMARY KEY(pool_id,raw_key),UNIQUE(pool_id,rank)) WITHOUT ROWID')
    yield ('CREATE TABLE recommendation_radio_bands (pool_id INTEGER NOT NULL,recording_id INTEGER NOT NULL,'
           "intensity_band INTEGER NOT NULL CHECK(typeof(intensity_band)='integer' AND intensity_band BETWEEN 0 AND 50),"
           'genre_key TEXT NOT NULL,PRIMARY KEY(pool_id,recording_id),FOREIGN KEY(pool_id,recording_id) '
           'REFERENCES recommendation_candidates(pool_id,recording_id)) WITHOUT ROWID')
    yield ('CREATE INDEX ix_recommendation_radio_band ON recommendation_radio_bands('
           'pool_id,intensity_band,genre_key,recording_id)')
    yield ('CREATE INDEX ix_recommendation_radio_genre ON recommendation_radio_bands('
           'pool_id,genre_key,intensity_band,recording_id)')
    yield ('CREATE TABLE recommendation_surface_pools (generation_id INTEGER NOT NULL,surface TEXT NOT NULL,'
           'mode_key TEXT NOT NULL,purpose TEXT NOT NULL CHECK(length(purpose)>0),'
           'pool_id INTEGER NOT NULL REFERENCES recommendation_pools(id),'
           'PRIMARY KEY(generation_id,surface,mode_key,purpose),FOREIGN KEY(generation_id,surface) '
           'REFERENCES recommendation_generation_surfaces(generation_id,surface)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_recommendation_surface_pool ON recommendation_surface_pools(pool_id,generation_id)'
    yield ('CREATE TABLE prepared_outputs (id INTEGER PRIMARY KEY AUTOINCREMENT,generation_id INTEGER NOT NULL,'
           'surface TEXT NOT NULL,chunk_key TEXT NOT NULL,filename TEXT NOT NULL CHECK(length(filename)>0),'
           'content_digest TEXT NOT NULL ' + _digest('content_digest') + ','
           "byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),"
           "format_version INTEGER NOT NULL CHECK(typeof(format_version)='integer' AND format_version=1),"
           'built_at REAL NOT NULL ' + _finite('built_at') + ',expires_at REAL '
           + _finite('expires_at', nullable=True) + ',pool_id INTEGER REFERENCES recommendation_pools(id),'
           "first_rank INTEGER CHECK(first_rank IS NULL OR (typeof(first_rank)='integer' AND first_rank>=0)),"
           "last_rank INTEGER CHECK(last_rank IS NULL OR (typeof(last_rank)='integer' AND last_rank>=first_rank)),"
           'CHECK((pool_id IS NULL AND first_rank IS NULL AND last_rank IS NULL) OR '
           '(pool_id IS NOT NULL AND first_rank IS NOT NULL AND last_rank IS NOT NULL)),'
           'UNIQUE(generation_id,surface,chunk_key),'
           'FOREIGN KEY(generation_id,surface) REFERENCES recommendation_generation_surfaces(generation_id,surface))')
    yield 'CREATE INDEX ix_prepared_output_expiry ON prepared_outputs(expires_at,id) WHERE expires_at IS NOT NULL'
    yield ('CREATE UNIQUE INDEX ix_prepared_output_pool_rank ON prepared_outputs('
           'generation_id,surface,pool_id,first_rank) WHERE pool_id IS NOT NULL')
    yield 'CREATE INDEX ix_prepared_output_pool ON prepared_outputs(pool_id,id) WHERE pool_id IS NOT NULL'
    yield ('CREATE TRIGGER selected_prepared_output_pool BEFORE INSERT ON prepared_outputs '
           'WHEN NEW.pool_id IS NOT NULL AND (NOT EXISTS(SELECT 1 FROM recommendation_surface_pools m '
           'JOIN recommendation_pools p ON p.id=m.pool_id WHERE m.generation_id=NEW.generation_id '
           'AND m.surface=NEW.surface AND m.pool_id=NEW.pool_id AND NEW.last_rank<p.eligible_count) OR '
           'EXISTS(SELECT 1 FROM prepared_outputs o WHERE o.generation_id=NEW.generation_id '
           'AND o.surface=NEW.surface AND o.pool_id=NEW.pool_id '
           'AND o.first_rank<=NEW.last_rank AND o.last_rank>=NEW.first_rank)) '
           "BEGIN SELECT RAISE(ABORT,'Display chunk requires disjoint ranks from a selected pool'); END")
    yield ('CREATE TABLE recommendation_generation_refs (kind TEXT NOT NULL CHECK(kind IN '
           "('offer','response','prepared','comparison','pending','checkpoint')),external_key TEXT NOT NULL "
           'CHECK(length(external_key)>0),generation_id INTEGER NOT NULL REFERENCES recommendation_generations(id),'
           'PRIMARY KEY(kind,external_key,generation_id)) WITHOUT ROWID')
    yield ('CREATE INDEX ix_recommendation_generation_ref ON recommendation_generation_refs('
           'generation_id,kind,external_key)')
    owners = ','.join("'proposal:'||OLD.surface||':" + field + "'" for field in
                      ('context', 'choice_context', 'album_choice_context', 'recommendations'))
    yield ('CREATE TRIGGER retired_proposal_recommendation_references BEFORE DELETE ON rec_proposals BEGIN '
           "DELETE FROM recommendation_generation_refs WHERE kind IN ('prepared','comparison') "
           f'AND external_key IN ({owners}); END')
    yield ('CREATE TRIGGER immutable_recommendation_generation BEFORE UPDATE OF id,external_key,policy_version,'
           'group_generation_id,output_digest,bundle_required,input_revision,revision_reason,created_at ON recommendation_generations '
           "BEGIN SELECT RAISE(ABORT,'Recommendation generation inputs are immutable'); END")
    yield ('CREATE TRIGGER new_recommendation_generation BEFORE INSERT ON recommendation_generations '
           "WHEN NEW.status<>'building' BEGIN SELECT RAISE(ABORT,'Recommendation publication must be built privately'); END")
    yield ('CREATE TRIGGER recommendation_generation_transition BEFORE UPDATE OF status,published_at '
           'ON recommendation_generations WHEN NOT ('
           "(OLD.status='building' AND NEW.status='ready' AND NEW.published_at IS NULL) OR "
           "(OLD.status='ready' AND NEW.status='published' AND NEW.published_at IS NOT NULL) OR "
           "(OLD.status IN ('building','ready','published') AND NEW.status='retiring' "
           'AND NEW.published_at IS OLD.published_at AND NOT EXISTS('
           'SELECT 1 FROM recommendation_heads WHERE generation_id=OLD.id) AND NOT EXISTS('
           'SELECT 1 FROM recommendation_generation_refs WHERE generation_id=OLD.id))) '
           "BEGIN SELECT RAISE(ABORT,'Invalid recommendation publication transition'); END")
    yield ('CREATE TRIGGER complete_recommendation_generation BEFORE UPDATE OF status ON recommendation_generations '
           "WHEN NEW.status='ready' AND (NOT EXISTS(SELECT 1 FROM recommendation_generation_surfaces WHERE generation_id=OLD.id) "
           'OR (OLD.bundle_required=1 AND NOT EXISTS(SELECT 1 FROM recommendation_bundle_framing WHERE generation_id=OLD.id)) '
           'OR EXISTS(SELECT 1 FROM recommendation_generation_surfaces s WHERE s.generation_id=OLD.id AND ('
           's.pool_count<>(SELECT count(*) FROM recommendation_surface_pools m WHERE m.generation_id=s.generation_id '
           'AND m.surface=s.surface) OR s.output_count<>(SELECT count(*) FROM prepared_outputs o '
           'WHERE o.generation_id=s.generation_id AND o.surface=s.surface)))) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation publication is incomplete'); END")
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER published_recommendation_head_{event.lower()} BEFORE {event} ON recommendation_heads '
               "WHEN NOT EXISTS(SELECT 1 FROM recommendation_generations WHERE id=NEW.generation_id AND status='published') "
               "BEGIN SELECT RAISE(ABORT,'Recommendation head requires published output'); END")
    yield ('CREATE TRIGGER retained_recommendation_generation BEFORE DELETE ON recommendation_generations '
           "WHEN OLD.status<>'retiring' BEGIN SELECT RAISE(ABORT,'Recommendation retirement must be fenced'); END")
    for table in ('recommendation_generation_surfaces', 'recommendation_surface_pools',
                  'recommendation_bundle_framing', 'prepared_outputs'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
            allowed = "('building','retiring')" if event == 'DELETE' else "('building')"
            condition = ' OR '.join('EXISTS(SELECT 1 FROM recommendation_generations WHERE id='
                + side + '.generation_id AND status NOT IN ' + allowed + ')' for side in sides)
            # Verified restore can relocate filenames without changing output
            # identity or any other owner field, even for published output.
            if table == 'prepared_outputs' and event == 'UPDATE':
                condition = '(' + condition + ') AND (' + ' OR '.join(
                    f'NEW.{name} IS NOT OLD.{name}' for name in ('id', 'generation_id', 'surface', 'chunk_key',
                    'content_digest', 'byte_size', 'format_version', 'built_at', 'expires_at',
                    'pool_id', 'first_rank', 'last_rank')) + ')'
            yield (f'CREATE TRIGGER frozen_{table}_{event.lower()} BEFORE {event} ON {table} WHEN {condition} '
                   "BEGIN SELECT RAISE(ABORT,'Recommendation publication content is immutable'); END")
    yield ('CREATE TRIGGER immutable_recommendation_pool BEFORE UPDATE OF id,pool_key,policy_version,group_version_id,'
           'input_fingerprint,content_digest,purpose,eligible_count ON recommendation_pools '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool content is immutable'); END")
    yield ('CREATE TRIGGER new_recommendation_pool BEFORE INSERT ON recommendation_pools WHEN NEW.complete<>0 '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool must be built privately'); END")
    yield ('CREATE TRIGGER complete_recommendation_pool BEFORE UPDATE OF complete ON recommendation_pools WHEN '
           'OLD.complete=1 OR NEW.complete<>1 OR '
           'NEW.eligible_count<>(SELECT count(*) FROM recommendation_candidates WHERE pool_id=OLD.id)'
           '+(SELECT count(*) FROM unresolved_recommendation_candidates WHERE pool_id=OLD.id) OR '
           '(NEW.eligible_count>0 AND ((SELECT min(rank) FROM (SELECT rank FROM recommendation_candidates WHERE pool_id=OLD.id '
           'UNION ALL SELECT rank FROM unresolved_recommendation_candidates WHERE pool_id=OLD.id))<>0 OR '
           '(SELECT max(rank) FROM (SELECT rank FROM recommendation_candidates WHERE pool_id=OLD.id UNION ALL '
           'SELECT rank FROM unresolved_recommendation_candidates WHERE pool_id=OLD.id))<>NEW.eligible_count-1)) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool membership is incomplete or sealed'); END")
    for table in ('recommendation_candidates', 'unresolved_recommendation_candidates', 'recommendation_radio_bands'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
            condition = ' OR '.join('EXISTS(SELECT 1 FROM recommendation_pools p WHERE p.id='
                + side + '.pool_id AND p.complete=1' + (' AND (EXISTS(SELECT 1 FROM recommendation_surface_pools m '
                'WHERE m.pool_id=p.id) OR EXISTS(SELECT 1 FROM recommendation_build_pools b '
                'WHERE b.pool_id=p.id))' if event == 'DELETE' else '') + ')' for side in sides)
            yield (f'CREATE TRIGGER frozen_{table}_{event.lower()} BEFORE {event} ON {table} WHEN {condition} '
                   "BEGIN SELECT RAISE(ABORT,'Recommendation candidate content is immutable'); END")
    yield ('CREATE TRIGGER complete_recommendation_pool_selection BEFORE INSERT ON recommendation_surface_pools '
           'WHEN EXISTS(SELECT 1 FROM recommendation_pool_retirements WHERE pool_id=NEW.pool_id) OR '
           'NOT EXISTS(SELECT 1 FROM recommendation_pools p JOIN recommendation_generations g '
           'ON g.id=NEW.generation_id JOIN recommendation_policies pp ON pp.version=p.policy_version '
           'JOIN recommendation_policies gp ON gp.version=g.policy_version WHERE p.id=NEW.pool_id AND p.complete=1 '
           'AND COALESCE(pp.base_version,pp.version)=COALESCE(gp.base_version,gp.version) '
           'AND (p.group_version_id IS NULL OR EXISTS(SELECT 1 FROM generation_groups x '
           'WHERE x.generation_id=g.group_generation_id AND x.group_version_id=p.group_version_id))) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool has incompatible publication inputs'); END")
    yield ('CREATE TRIGGER immutable_recommendation_pool_selection BEFORE UPDATE ON recommendation_surface_pools '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool selection is immutable'); END")
    yield ('CREATE TRIGGER immutable_recommendation_generation_ref BEFORE UPDATE ON recommendation_generation_refs '
           "BEGIN SELECT RAISE(ABORT,'Recommendation generation references are immutable'); END")
    yield ('CREATE TRIGGER live_recommendation_generation_ref BEFORE INSERT ON recommendation_generation_refs '
           "WHEN NOT EXISTS(SELECT 1 FROM recommendation_generations WHERE id=NEW.generation_id AND status='published') "
           "BEGIN SELECT RAISE(ABORT,'Recommendation evidence requires a published generation'); END")
    yield ('CREATE TRIGGER retained_recommendation_group BEFORE DELETE ON group_publications '
           'WHEN EXISTS(SELECT 1 FROM recommendation_generations WHERE group_generation_id=OLD.generation_id) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation publication retains its musical groups'); END")
    yield ('CREATE TRIGGER retained_recommendation_pool BEFORE INSERT ON recommendation_pool_retirements '
           'WHEN EXISTS(SELECT 1 FROM recommendation_surface_pools WHERE pool_id=NEW.pool_id) OR '
           'EXISTS(SELECT 1 FROM recommendation_build_pools WHERE pool_id=NEW.pool_id) OR '
           'EXISTS(SELECT 1 FROM prepared_outputs WHERE pool_id=NEW.pool_id) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool still has an owner'); END")
    yield ('CREATE TRIGGER live_recommendation_build_pool BEFORE INSERT ON recommendation_build_pools '
           'WHEN EXISTS(SELECT 1 FROM recommendation_pool_retirements WHERE pool_id=NEW.pool_id) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool is being retired'); END")
    yield ('CREATE TRIGGER private_recommendation_build_pool BEFORE INSERT ON recommendation_build_pools '
           'WHEN EXISTS(SELECT 1 FROM recommendation_pools p WHERE p.id=NEW.pool_id AND p.complete=0 AND EXISTS('
           'SELECT 1 FROM recommendation_build_pools b WHERE b.pool_id=p.id AND b.generation_id<>NEW.generation_id)) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool is leased by another build'); END")
    yield ('CREATE TRIGGER recommendation_radio_band_owner BEFORE INSERT ON recommendation_radio_bands '
           "WHEN NOT EXISTS(SELECT 1 FROM recommendation_pools WHERE id=NEW.pool_id AND purpose='radio') "
           "BEGIN SELECT RAISE(ABORT,'Radio bands require a radio pool'); END")
    yield ('CREATE TRIGGER unresolved_recommendation_radio_band_owner BEFORE INSERT ON unresolved_recommendation_candidates '
           "WHEN NEW.intensity_band IS NOT NULL AND NOT EXISTS(SELECT 1 FROM recommendation_pools WHERE id=NEW.pool_id AND purpose='radio') "
           "BEGIN SELECT RAISE(ABORT,'Radio bands require a radio pool'); END")
    for table, other in (('recommendation_candidates', 'unresolved_recommendation_candidates'),
                         ('unresolved_recommendation_candidates', 'recommendation_candidates')):
        for event in ('INSERT', 'UPDATE'):
            yield (f'CREATE TRIGGER distinct_{table}_rank_{event.lower()} BEFORE {event} ON {table} '
                   f'WHEN EXISTS(SELECT 1 FROM {other} WHERE pool_id=NEW.pool_id AND rank=NEW.rank) '
                   "BEGIN SELECT RAISE(ABORT,'Recommendation pool ranks must be unique'); END")


def _legacy_plan(conn):
    from yt_playlist.core import json_cache
    from yt_playlist.rec import bundle_storage
    from yt_playlist.repos import proposal_cache

    values, timestamps, identities = {}, [], []
    known = {row[0] for row in conn.execute('SELECT identity_key FROM recordings')}
    for surface in proposal_cache.SURFACES:
        head = conn.execute('SELECT cache_id,built_at FROM rec_proposal_cache_heads WHERE surface=?',
                            (surface,)).fetchone()
        if head:
            raw = proposal_cache.read(conn, surface)
            built = head[1]
        else:
            row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
            if row is None:
                continue
            raw, built = row
        if len(raw.encode()) > bundle_storage.publication.MAX_PLAN_BYTES:
            raise ValueError('Legacy candidate source exceeds its migration byte budget')
        values[surface] = json_cache.decode(raw, profile_fields=('digest',),
                                           preserve_profiles=lambda item: item.get('key') not in known,
                                           max_bytes=bundle_storage.publication.MAX_PLAN_BYTES)
        timestamps.append(built)
        identities.append((surface, hashlib.sha256(raw.encode()).hexdigest(), built))
    if not values or all(value is None for value in values.values()):
        return None
    bundle, fresh = values.get('mode_bundles') or {}, values.get('fresh_pool')
    if not isinstance(bundle, dict) or fresh is not None and not isinstance(fresh, list):
        raise ValueError('Invalid original prepared candidate collection')
    # The old producer stamped each ranked pool, but recorded neither a bundle
    # policy nor a prepared-input serial. Preserve that absence. A generation
    # owner can use an existing candidate policy only when every candidate has
    # the same recorded input family; it cannot adopt today's serving policy.
    versions = set()
    def candidates(value):
        if isinstance(value, list):
            for item in value:
                candidates(item)
        elif isinstance(value, dict):
            if 'pool_policy' in value:
                version = value['pool_policy']
                if not isinstance(version, str) or not version:
                    raise ValueError('Invalid original candidate policy')
                versions.add(version)
            else:
                for field, item in value.items():
                    if field != 'musical':
                        candidates(item)
    candidates(bundle)
    candidates(fresh)
    if not versions:
        # Empty historical output has no captured ranking policy. Do not give
        # it one merely to create an empty head; ordinary preparation can start.
        if any(value for key, value in bundle.items() if key != '_meta') or fresh:
            raise ValueError('Original candidate output has no recorded policy')
        return None
    families = set()
    for version in sorted(versions):
        row = conn.execute('SELECT COALESCE(base_version,version) FROM recommendation_policies WHERE version=?',
                           (version,)).fetchone()
        if row is None:
            raise ValueError('Missing original candidate policy: ' + version)
        families.add(row[0])
    if len(families) != 1:
        raise ValueError('Original candidate pools have different recorded input families')
    from yt_playlist.core.evidence_ledger import canonical
    key = 'legacy-bundle:' + hashlib.sha256(canonical(identities)).hexdigest()
    plan = bundle_storage.prepare(conn, bundle, fresh, external_key=key, policy_version=min(versions),
                                  input_revision=None, created_at=max(timestamps))
    if values.get('mode_bundles') is None:
        plan = replace(plan, outputs=tuple(output for output in plan.outputs if output.surface != 'home'),
                       framing=None)
    return plan


def apply(conn, context):
    from yt_playlist.repos import recommendation_publication

    try:
        plan = _legacy_plan(conn)
    except (ValueError, TypeError, KeyError) as exc:
        raise MigrationError(f'Invalid original recommendation candidates: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    if plan is not None:
        recommendation_publication.import_plan(conn, plan, plan.created_at)
    conn.execute('DROP TRIGGER proposal_cache_legacy_update')
    conn.execute("DELETE FROM rec_proposals WHERE surface IN ('mode_bundles','fresh_pool')")
    for table in ('rec_proposal_cache_heads', 'rec_proposal_cache_chunks', 'rec_proposal_cache_builds'):
        conn.execute(f'DROP TABLE {table}')


def validate(conn):
    from .current import _tokens
    for sql in statements():
        words = sql.split()
        name = words[3] if words[1] == 'UNIQUE' else words[2]
        actual = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible recommendation publication structure: {name}')
    for table in ('rec_proposal_cache_heads', 'rec_proposal_cache_chunks', 'rec_proposal_cache_builds'):
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
            raise MigrationError(f'Superseded candidate cache remains: {table}')
