"""Home mix cache: file preparation outside the SQL publication transaction."""
from contextlib import contextmanager, ExitStack, nullcontext
from dataclasses import dataclass
import hashlib

from yt_playlist.core import fao, home_outputs, json_cache, policy_documents, prepared_references
from yt_playlist.core import recommendation_outputs as files
from yt_playlist.core import recommendation_references
from yt_playlist.core.db_writes import atomic_write

SURFACES = frozenset({'home_prepared', 'home_tuned', 'home_choice'})


@dataclass(frozen=True)
class Publication:
    prepared: home_outputs.Prepared
    filenames: tuple
    policies: frozenset
    contexts: dict
    recommendation_generations: tuple | None


def root(store):
    return files.root_for_database(store._db_path, store._evidence_key)


@contextmanager
def writing(store):
    # Collection already uses this shared lifetime lease. Acquire it before
    # creating files, and keep it until their SQL heads have committed.
    directories = [root(store)]
    if not store._home_display_files and store._db_path is not None:
        database = store._db_path
        directories.append(database.with_name(database.name + '.preparation') / 'home-outputs')
    with ExitStack() as retained:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            retained.enter_context(policy_documents.retained_reader(directory))
        yield


def prepare(store, surface, raw, now):
    """Prepare inside writing(), before entering the SQL publication transaction."""
    if surface not in SURFACES:
        raise ValueError('Unknown Home mix cache surface')
    prepared = home_outputs.prepare(surface, raw, now)
    contexts = {}
    if isinstance(prepared.value, dict) and prepared.value.get('context') and store._compact_group_files:
        contexts['context'] = prepared_references.context(store, prepared.value['context'])
    generations = (tuple(recommendation_references.prepared_generations(
        surface, prepared.value if isinstance(prepared.value, dict) else {}))
        if store._recommendation_files else None)
    filenames = []
    if store._home_display_files:
        for position, encoded in enumerate(prepared.parts):
            digest = hashlib.sha256(encoded).hexdigest()
            path = root(store) / digest[:2] / (digest + '.json')
            fao.write(path, encoded)
            filenames.append((surface, str(position), str(path), digest, len(encoded)))
    return Publication(prepared, tuple(filenames), frozenset(home_outputs.policy_references(prepared.value)),
                       contexts, generations)


def publish(store, publication, *, input_revision=None):
    return store.home_cache.publish(publication.prepared, publication.filenames,
        publication.policies, publication.contexts, publication.recommendation_generations,
        input_revision=input_revision)


def put(store, surface, value, now=None):
    with writing(store):
        publication = prepare(store, surface, json_cache.encode(value, shared_profiles=False), now)
        with atomic_write(store):
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
            raw, dependencies, _ = cache[surface]
            if reads is not None:
                reads.include(dependencies)
            return raw
        with reads.capture() if reads is not None else nullcontext(set()) as dependencies:
            raw, manifest = store.home_cache.document(surface)
            if manifest is not None:
                raw = home_outputs.read_manifest(surface, *manifest)
        size = len((raw or '').encode())
        if cache is not None and sum(entry[2] for entry in cache.values()) + size <= home_outputs.MAX_BYTES:
            cache[surface] = raw, frozenset(dependencies), size
        return raw


def read(store, surface, *, fields=None):
    raw = encoded(store, surface)
    return json_cache.decode(raw, keys=set(fields) if fields is not None else None) if raw is not None else None
