"""Content-addressed policy bodies shared by compact, immutable ranking variants."""
import hashlib
import json

# Disposable preparation is bounded separately from the served ledger.
GRACE_SECONDS = 3600
SCAN_LIMIT = 4096
SCAN_BYTES = 8 * 1024 * 1024
MAX_UNUSED_BYTES = 64 * 1024 * 1024
MAX_UNUSED_VARIANTS = 131072
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_BATCH_VARIANTS = 16384
CURSOR = 'rec_unused_policy_cursor'
HISTORY_BATCH = 128
HISTORY_RETAIN_DAYS = 180
HISTORY_FILE_GRACE_DAYS = 30
HISTORY_FILE_SCAN_LIMIT = 16


class PolicyStorageFull(RuntimeError):
    pass


class PolicyCapacityFull(PolicyStorageFull):
    """A valid batch can be retried after unused-policy maintenance makes room."""
    def __init__(self, count, byte_size, added_count, added_bytes):
        super().__init__(f'Unused recommendation policies reached their storage budget: '
            f'{count} variants, {byte_size} bytes; batch adds {added_count} variants and '
            f'{added_bytes} bytes. Retaining published pools until maintenance frees space')

BASE_FIELD = '_policy_base'
HOME_CONTEXTS = ('context', 'choice_context', 'album_choice_context')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def base_record(configuration, now):
    body = {key: value for key, value in configuration.items() if key not in ('ranker', 'experiment')}
    raw = canonical(body)
    version = hashlib.sha256(raw.encode()).hexdigest()
    return version, raw, len(raw.encode()), now


def variant_record(base, dimensions, now, *, version=None):
    raw = canonical({BASE_FIELD: base, **dimensions})
    return version or hashlib.sha256(raw.encode()).hexdigest()[:16], raw, now


def pack(configuration, now, *, version=None):
    base = base_record(configuration, now)
    dimensions = {key: configuration[key] for key in ('ranker', 'experiment') if key in configuration}
    return variant_record(base[0], dimensions, now, version=version), base


def home_references(value):
    """Only declared Home contexts, offers and their tracks carry policy identity."""
    if not isinstance(value, dict):
        return set()
    result = set()

    def capture(item):
        if not isinstance(item, dict):
            return
        for field in ('model_version', 'pool_policy'):
            version = item.get(field)
            if version is not None:
                if not isinstance(version, (str, int)) or isinstance(version, bool) or not str(version):
                    raise ValueError('Invalid explicit Home policy reference')
                result.add(str(version))

    for field in HOME_CONTEXTS:
        capture(value.get(field))
    options = list(value.get('protos', []))
    menus = value.get('menus', [])
    if isinstance(menus, list):
        if len(menus) > 128:
            raise ValueError('Home menu reference budget exceeded')
        for menu in menus:
            if isinstance(menu, dict):
                options.extend(menu.get('protos', []))
    held = value.get('menu')
    if isinstance(held, dict):
        options.extend(held.get('protos', []))
    if len(options) > 8192:
        raise ValueError('Home offer reference budget exceeded')
    tracks = []
    for option in options:
        capture(option)
        if isinstance(option, dict):
            capture(option.get('context'))
            tracks.extend(option.get('tracks', []))
    for field in ('tracks', 'discovery_tracks', 'rediscover_tracks'):
        tracks.extend(value.get(field, []))
    items = value.get('items', [])
    if not isinstance(items, list) or len(items) > 512:
        raise ValueError('Home album reference budget exceeded')
    for item in items:
        if isinstance(item, dict):
            tracks.extend(item.get('companion', []))
    if len(tracks) > 32768:
        raise ValueError('Home track reference budget exceeded')
    for track in tracks:
        capture(track)
        if isinstance(track, dict):
            capture(track.get('serving_context'))
            capture(track.get('provenance'))
    return result


def references(value):
    """Policy references at the finite paths in predecessor serving messages."""

    def capture(item):
        if not isinstance(item, dict):
            return
        for field in ('model_version', 'pool_policy'):
            version = item.get(field)
            if type(version) in (str, int) and str(version):
                result.add(str(version))

    def track(item):
        capture(item)
        if isinstance(item, dict):
            capture(item.get('serving_context'))
            capture(item.get('provenance'))

    result = set()
    if isinstance(value, list):
        for item in value:
            track(item)
        yield from sorted(result)
        return
    if not isinstance(value, dict):
        return
    documents = [value]
    if isinstance(value.get('data'), dict):
        documents.append(value['data'])
    for document in documents:
        result.update(home_references(document))
        capture(document)
        if type(document.get('policy_version')) is str and document['policy_version']:
            result.add(document['policy_version'])
        for item in document.get('policies', []):
            capture(item)
        bundle = document.get('bundles', document)
        if not isinstance(bundle, dict):
            continue
        for mode, lanes in bundle.items():
            if not mode.startswith('_') and isinstance(lanes, dict):
                for pool in lanes.values():
                    if isinstance(pool, list):
                        for item in pool:
                            track(item)
        vibes = bundle.get('_vibes', {})
        if isinstance(vibes, dict):
            for lanes in vibes.get('pools', {}).values():
                pools = [lanes] if isinstance(lanes, list) else lanes.values() if isinstance(lanes, dict) else []
                for pool in pools:
                    if isinstance(pool, list):
                        for item in pool:
                            track(item)
            for lanes in vibes.get('genres', {}).values():
                if isinstance(lanes, dict):
                    for genres in lanes.values():
                        if isinstance(genres, dict):
                            for pool in genres.values():
                                if isinstance(pool, list):
                                    for item in pool:
                                        track(item)
    yield from sorted(result)


def protected_references(messages):
    protected = set()
    for raw in messages:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = raw
        protected.update(references(value))
    return protected
