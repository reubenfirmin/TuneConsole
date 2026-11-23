"""Home mix cache: file preparation outside the SQL publication transaction."""
from contextlib import contextmanager, nullcontext

from yt_playlist.core import home_outputs, json_cache, policy_documents
from yt_playlist.core import recommendation_outputs as files
from yt_playlist.core.db_writes import atomic_write

SURFACES = frozenset({'home_prepared', 'home_tuned', 'home_choice'})


def root(store):
    return files.root_for_database(store._db_path, store._evidence_key)


@contextmanager
def writing(store):
    # Collection already uses this shared lifetime lease. Acquire it before
    # creating files, and keep it until their SQL heads have committed.
    directory = root(store)
    directory.mkdir(parents=True, exist_ok=True)
    with policy_documents.retained_reader(directory):
        yield


def prepare(store, surface, raw, now):
    """Prepare inside writing(), before entering the SQL publication transaction."""
    if surface not in SURFACES:
        raise ValueError('Unknown Home mix cache surface')
    return store.home_cache.prepare(surface, raw, now, cache=True)


def publish(store, publication, *, input_revision=None):
    return store.home_cache.publish(publication, input_revision=input_revision)


def put(store, surface, value, now=None, *, before_publish=None):
    with writing(store):
        publication = prepare(store, surface, json_cache.encode(value, shared_profiles=False), now)
        with atomic_write(store):
            if before_publish is None or before_publish():
                publish(store, publication)


def remember(store, tailored, held, now, *, before_publish=None):
    with writing(store):
        publications = [prepare(store, surface, json_cache.encode(value, shared_profiles=False), now)
                        for surface, value in (('home_tuned', tailored), ('home_choice', held))]
        with atomic_write(store):
            if before_publish is not None and not before_publish():
                return False
            for publication in publications:
                publish(store, publication)
        return True


@contextmanager
def verified_reads(store):
    """Reuse immutable documents only within one pinned response."""
    if not getattr(store, '_read_only', False):
        raise ValueError('Verified Home reads require a pinned read-only store')
    previous = getattr(store, '_home_cache_reads', None)
    store._home_cache_reads = previous if previous is not None else {}
    try:
        yield
    finally:
        store._home_cache_reads = previous


def encoded(store, surface):
    if surface not in SURFACES:
        raise ValueError('Unknown Home mix cache surface')
    # Own the file lifetime before capturing SQL metadata, including when this
    # standalone read opens a private SQL snapshot inside the repository.
    with writing(store):
        cache = getattr(store, '_home_cache_reads', None)
        reads = getattr(store, '_snapshot_reads', None)
        if cache is not None and surface in cache:
            raw, dependencies, _, _ = cache[surface]
            if reads is not None:
                reads.include(dependencies)
            return raw
        with reads.capture() if reads is not None else nullcontext(set()) as dependencies:
            manifest = store.home_cache.document(surface)
            raw = None if manifest is None else home_outputs.read_manifest(
                surface, manifest.digest, manifest.size, manifest.parts)
        size = len((raw or '').encode())
        if cache is not None and sum(entry[2] for entry in cache.values()) + size <= home_outputs.MAX_BYTES:
            cache[surface] = raw, frozenset(dependencies), size, manifest.publication_id if manifest else None
        return raw


def read(store, surface, *, fields=None):
    raw = encoded(store, surface)
    return json_cache.decode(raw, keys=set(fields) if fields is not None else None) if raw is not None else None


def menu_index(store, surface):
    with writing(store):
        manifest = store.home_cache.document(surface)
        if manifest is None or manifest.schema != json_cache.HOME_MENU_SCHEMA or not manifest.parts:
            return None
        header, = home_outputs.read_parts(surface, manifest.parts[:1])
        if not header.startswith('{"' + json_cache.MARKER + '":"' + json_cache.HOME_MENUS + '"'):
            return None
        index, counts = json_cache.home_menu_index(header, max_bytes=home_outputs.MAX_DECODED_BYTES)
        if sum(counts) + 1 != len(manifest.parts):
            raise ValueError('Incomplete Home display menus')

    def restore(position):
        start = 1 + sum(counts[:position])
        with writing(store):
            return json_cache.decode_home_menu(
                home_outputs.read_parts(surface, manifest.parts[start:start + counts[position]]),
                max_bytes=home_outputs.MAX_DECODED_BYTES)

    return index, restore


def matches_current(store, surface, value, *, reader, observed):
    """Compare against the current document, reusing a verified read of identical parts."""
    cached = (getattr(reader, '_home_cache_reads', None) or {}).get(surface)
    if cached is not None and store.home_cache.publication_id(surface) == cached[3]:
        return value == observed
    return value == read(store, surface)


def observed_publication_id(reader, surface):
    cached = (getattr(reader, '_home_cache_reads', None) or {}).get(surface)
    return reader.home_cache.publication_id(surface) if cached is None else cached[3]
