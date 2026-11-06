"""Decompose the established mode-bundle message into explicit ranked slots.

This boundary accepts one privately calculated or legacy bundle. Catalog fields
are resolved by canonical recording ownership. Only generated explanations and
unresolved historical rows enter bounded opaque output chunks.
"""
import hashlib
from dataclasses import replace

from yt_playlist.core import group_content, policy_documents, recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.core.policy_config import BASE_FIELD
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos import policy_storage, recommendation_publication as publication

LANES = frozenset({'wheelhouse', 'explore', 'comfort', 'fresh', 'temporal'})
ITEM_FIELDS = frozenset({'key', 'video_id', 'title', 'artist', 'album', 'thumbnail', 'plays', 'reason',
                        'lane', 'genre', 'year', 'score', 'pool_policy', 'musical'})
EXPLANATION_BYTES = 64 * 1024
REUSE_GENERATIONS = 4


def _recent_pools(conn):
    """Bound lookup to recent selections, including completed staged work.

    These are comparison inputs, not ranking dependencies. The caller retains
    this reader until every selected immutable policy/body has been captured.
    """
    previous = {}
    for row in conn.execute(
            'SELECT m.surface,m.mode_key,m.purpose,p.id,p.pool_key,p.policy_version,'
            'p.group_version_id,p.input_fingerprint,p.purpose,p.eligible_count,p.content_digest '
            "FROM (SELECT id FROM recommendation_generations WHERE status!='retiring' ORDER BY id DESC LIMIT ?) g "
            'JOIN recommendation_surface_pools m ON m.generation_id=g.id '
            'JOIN recommendation_pools p ON p.id=m.pool_id WHERE p.complete=1 '
            'AND NOT EXISTS(SELECT 1 FROM recommendation_pool_retirements r WHERE r.pool_id=p.id) '
            'ORDER BY g.id DESC LIMIT ?', (REUSE_GENERATIONS, REUSE_GENERATIONS * publication.MAX_SLOTS)):
        previous.setdefault(tuple(row[:3]), []).append(tuple(row[3:]))
    return previous


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    return value


def _keys(value, allowed, label):
    _object(value, label)
    if value.keys() - allowed:
        raise ValueError(f'Unsupported {label} fields: ' + ', '.join(sorted(value.keys() - allowed)))


def _framing(bundle):
    meta = _object(bundle.get('_meta', {}), 'Bundle metadata')
    radio = _object(bundle.get('_radio', {}), 'Radio catalog')
    intensity = _object(radio.get('intensity', {}), 'Radio intensity index')
    vibes = _object(bundle.get('_vibes', {}), 'Vibe pools')
    _keys(radio, {'generation', 'space', 'members', 'scores', 'intensity'}, 'radio catalog')
    _keys(intensity, {'version', 'profile_version', 'clusters'}, 'radio intensity index')
    _keys(vibes, {'version', 'profile_version', 'pools', 'genres'}, 'vibe pools')
    fields = [(meta, 'comfort_pool'), (meta, 'year_cuts'), (meta, 'full_pools'), (meta, 'unmodeled'),
              (meta, 'musical_generation'), (radio, 'generation'), (radio, 'space'), (intensity, 'version'),
              (intensity, 'profile_version'), (vibes, 'version'), (vibes, 'profile_version')]
    presence = sum(1 << position for position, (owner, key) in enumerate(fields) if key in owner)
    years = meta.get('year_cuts')
    if years is not None and (not isinstance(years, list) or len(years) != 2):
        raise ValueError('Invalid prepared release-year cuts')
    framing = publication.Framing(presence, meta.get('comfort_pool'), years[0] if years else None,
        years[1] if years else None, meta.get('full_pools'), meta.get('unmodeled'), meta.get('musical_generation'),
        radio.get('generation'), radio.get('space'), intensity.get('version'), intensity.get('profile_version'),
        vibes.get('version'), vibes.get('profile_version'))
    # These metadata fields are opaque evidence. Every field used to query or
    # filter membership above has a typed owner instead of a JSON-only copy.
    opaque = {key: value for key, value in meta.items()
              if key not in {'comfort_pool', 'year_cuts', 'full_pools', 'unmodeled', 'musical_generation'}}
    return framing, opaque


def prepare(conn, bundle, fresh, *, external_key, policy_version, input_revision, created_at,
            group_generation_id=None, pending_policies=(), home_surface='home', surface_prefix='', allow_unobserved=False,
            reuse_existing=False, reuse_conn=None):
    _object(bundle, 'Mode bundle')
    framing, metadata = _framing(bundle)
    if any(key.startswith('_') and key not in {'_meta', '_radio', '_ppr', '_vibes'} for key in bundle):
        raise ValueError('Unsupported mode-bundle owner')
    slots, documents, recordings, policy_families, explanations, distinct = [], [], {}, {}, set(), set()
    reuse_existing = bool(reuse_existing and conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='recommendation_pool_revalidations' AND type='table'").fetchone())
    reuse_conn = conn if reuse_conn is None else reuse_conn
    recent = _recent_pools(reuse_conn) if reuse_existing else {}
    reused = {}
    admitted = 0
    # The producer calculates these compact policies on its private snapshot.
    # They are installed with the build's generation FK in one claim transaction,
    # so maintenance never sees an unowned gap between installation and claim.
    pending, pending_bytes = {}, 0
    for index, (version, raw, created) in enumerate(pending_policies):
        if index >= policy_storage.MAX_BATCH_VARIANTS:
            raise ValueError('Pending recommendation policies exceed their publication budget')
        value, digest = policy_documents._record(version, raw, created)
        pending_bytes += len(raw.encode())
        if pending_bytes > policy_storage.MAX_BATCH_BYTES:
            raise ValueError('Pending recommendation policies exceed their publication budget')
        previous = pending.get(version)
        if previous is not None and previous[1] != digest:
            raise ValueError('Conflicting pending recommendation policy')
        base = value.get(BASE_FIELD)
        if base is not None and (not isinstance(base, str) or not base):
            raise ValueError('Invalid pending recommendation policy base')
        pending[version] = (base or version, digest)

    def resolve_family(version):
        if version in pending:
            return pending[version][0]
        row = conn.execute('SELECT COALESCE(base_version,version) FROM recommendation_policies WHERE version=?',
                           (version,)).fetchone()
        if row is None:
            raise ValueError('Missing recommendation generation or candidate policy: ' + version)
        return row[0]

    root = resolve_family(policy_version)

    def recording(key):
        if not isinstance(key, str) or not key:
            raise ValueError('Invalid exact recommendation recording key')
        if key not in recordings:
            row = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            recordings[key] = row[0] if row else None
        return recordings[key]

    def family(version):
        if version not in policy_families:
            policy_families[version] = resolve_family(version)
        if policy_families[version] != root:
            raise ValueError('Prepared candidate policies belong to different input generations')

    def chunks(surface, digest, values):
        pending, used = [], 0
        for value in values:
            size = len(canonical(value))
            if pending and used + size > EXPLANATION_BYTES:
                key = canonical(['pool-display', digest, pending[0][0]]).decode()
                document(surface, key, {'rows': pending}, digest, pending[0][0], pending[-1][0])
                pending, used = [], 0
            pending.append(value)
            used += size
        if pending:
            key = canonical(['pool-display', digest, pending[0][0]]).decode()
            document(surface, key, {'rows': pending}, digest, pending[0][0], pending[-1][0])

    def document(surface, key, value, pool_digest=None, first_rank=None, last_rank=None):
        nonlocal admitted
        surface = surface_prefix + (home_surface if surface == 'home' else surface)
        encoded = outputs.encode(surface, key, value)
        admitted += len(encoded)
        if admitted > publication.MAX_PLAN_BYTES or len(slots) + len(documents) >= publication.MAX_SLOTS:
            raise ValueError('Recommendation bundle exceeds its admitted publication budget')
        documents.append(publication.Output(surface, key, encoded, pool_digest=pool_digest,
                                             first_rank=first_rank, last_rank=last_rank))

    # The whole-bundle musical digest changes when an unrelated pool gains a
    # candidate. Native reuse hashes each local candidate's musical input below;
    # the complete musical generation remains immutable generation framing.
    dependencies = ([framing.radio_generation, framing.radio_space] if reuse_existing else
                    [framing.musical_generation, framing.radio_generation, framing.radio_space])

    def ranked(surface, mode, lane, values, purpose, *, bands=None, default_policy=None):
        nonlocal admitted
        if not isinstance(values, list):
            raise ValueError('Prepared candidates must be a ranked array')
        candidates, display, versions = [], [], set()
        inputs = hashlib.sha256(canonical([
            'prepared-pool-inputs-2' if reuse_existing else 'prepared-pool-inputs-1', dependencies]))
        for rank, value in enumerate(values):
            check_cancellable_work()
            _keys(value, ITEM_FIELDS, 'candidate')
            key = value.get('key')
            owner = recording(key)
            version = value.get('pool_policy') or default_policy
            if not isinstance(version, str) or not version:
                raise ValueError('Prepared candidate has no recorded policy')
            versions.add(version)
            band, genre = bands.get(key, (None, None)) if bands is not None else (None, None)
            candidate = (pools.Candidate(owner, value.get('score'), band, genre) if owner is not None
                         else pools.UnresolvedCandidate(key, value.get('score'), band, genre))
            candidates.append(candidate)
            # Old musical input evidence contributes its exact canonical digest;
            # the copied profile/sample history itself is never pool metadata.
            musical = value.get('musical')
            inputs.update(canonical([pools.owner(candidate), musical.get('digest') if isinstance(musical, dict) else None]))
            opaque = {field: value[field] for field in ('reason', 'lane') if field in value}
            if owner is None:
                opaque = {'unresolved_candidate': value, 'reason': 'missing_recording'}
            if opaque:
                display.append([rank, pools.owner(candidate), opaque])
        if len(versions) > 1:
            raise ValueError('One ranked pool contains conflicting policies')
        version = next(iter(versions), default_policy or policy_version)
        family(version)
        fingerprint = inputs.hexdigest()
        prepared = pools.prepare(lane, version, None, fingerprint, purpose, candidates)
        selected_surface = surface_prefix + (home_surface if surface == 'home' else surface)
        if reuse_existing and not any(isinstance(candidate, pools.UnresolvedCandidate) for candidate in candidates):
            identity = prepared.content_digest
            if identity in reused:
                prepared = reused[identity]
            else:
                expected = (lane, prepared.group_version_id, fingerprint, purpose, len(prepared.candidates))
                for old in recent.get((selected_surface, mode, lane), ()):
                    pool_id, key, old_policy, group, old_fingerprint, old_purpose, count, digest = old
                    if (key, group, old_fingerprint, old_purpose, count) != expected:
                        continue
                    candidate = replace(prepared, policy_version=old_policy, content_digest=digest)
                    if group_content.equal(pools.stored_rows(reuse_conn, pool_id), pools.rows(candidate)):
                        prepared = candidate
                        break
                reused[identity] = prepared
        if prepared.content_digest not in distinct:
            admitted += sum(len(row) for row in pools.rows(prepared))
            distinct.add(prepared.content_digest)
        if admitted > publication.MAX_PLAN_BYTES or len(slots) + len(documents) >= publication.MAX_SLOTS:
            raise ValueError('Recommendation bundle exceeds its admitted publication budget')
        slots.append(publication.Slot(selected_surface, mode, lane, prepared,
            revalidated_policy=version if prepared.policy_version != version else None))
        if display and (surface, prepared.content_digest) not in explanations:
            chunks(surface, prepared.content_digest, display)
            explanations.add((surface, prepared.content_digest))

    for mode, bucket in bundle.items():
        if mode.startswith('_'):
            continue
        _keys(bucket, LANES, 'mode bucket')
        for lane, candidates in bucket.items():
            ranked('home', mode, lane, candidates, 'temporal' if lane == 'temporal' else 'fresh' if lane == 'fresh' else 'card')
    if fresh is not None:
        ranked('fresh', 'all', 'fresh', fresh, 'fresh')
    radio = bundle.get('_radio', {})
    members = _object(radio.get('members', {}), 'Radio membership')
    clusters = _object(radio.get('intensity', {}).get('clusters', {}), 'Radio intensity clusters')
    scores = _object(radio.get('scores', {}), 'Radio catalog scores')
    if clusters.keys() - members.keys():
        raise ValueError('Radio intensity index has no matching member pool')
    for mode, keys in members.items():
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise ValueError('Invalid radio member pool')
        bands = {}
        for band, genres in _object(clusters.get(mode, {}), 'Radio intensity bands').items():
            if not isinstance(band, str) or not band.isascii() or not band.isdigit() or str(int(band)) != band or not 0 <= int(band) <= 50:
                raise ValueError('Invalid radio intensity band')
            for genre, owners in _object(genres, 'Radio genre bands').items():
                if not isinstance(owners, list) or not isinstance(genre, str):
                    raise ValueError('Invalid radio genre membership')
                for key in owners:
                    if not isinstance(key, str) or key in bands:
                        raise ValueError('Duplicate radio intensity owner')
                    bands[key] = int(band), genre
        if bands and set(bands) != set(keys):
            raise ValueError('Radio intensity index does not match complete membership')
        ranked('radio', mode, 'membership', [{'key': key, 'score': scores.get(key)} for key in keys],
               'radio', bands=bands, default_policy=policy_version)
    if scores:
        ranked('radio', 'all', 'scores', [{'key': key, 'score': score} for key, score in sorted(scores.items())],
               'radio', default_policy=policy_version)
    for mode, keys in _object(bundle.get('_ppr', {}), 'Mode PPR rankings').items():
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise ValueError('Invalid mode PPR ranking')
        ranked('radio', mode, 'ppr', [{'key': key} for key in keys], 'ppr', default_policy=policy_version)
    vibes = bundle.get('_vibes', {})
    for preset, lanes in _object(vibes.get('pools', {}), 'Vibe candidate pools').items():
        for lane, candidates in _object(lanes, 'Vibe lanes').items():
            ranked('vibe:' + preset, '', lane, candidates, 'vibe')
    for preset, lanes in _object(vibes.get('genres', {}), 'Vibe genre pools').items():
        for lane, genres in _object(lanes, 'Vibe genre lanes').items():
            for genre, candidates in _object(genres, 'Vibe genre membership').items():
                ranked('vibe:' + preset, genre, lane, candidates, 'vibe')
    if metadata:
        document('home', 'ranking-evidence', metadata)
    if (not any(slot.surface == surface_prefix + home_surface for slot in slots)
            and not any(output.surface == surface_prefix + home_surface for output in documents)):
        # An explicitly empty/unprepared bundle is meaningful last-good output.
        document('home', 'empty', {})
    prepared = publication.Plan(external_key, policy_version, group_generation_id, input_revision, created_at,
                                tuple(slots), tuple(documents), framing)
    publication.validate(prepared, allow_unobserved=allow_unobserved)
    return prepared
