"""Small slices on the existing preparation worker, with last-good publication."""
import json

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.rec import musical_profiles as profiles


def due(store):
    return bool(store.musical_profiles.pending() or any(store.musical_profiles.stale(profiles.VERSION, 1)))


def source_step(store, now, *, batch=32, seconds=2):
    store.musical_profiles.queue_stale(profiles.VERSION, batch)
    key = store.musical_profiles.pending()
    if not key:
        return False
    dependencies, observation_ids = store.musical_profiles.source_dependencies(key)
    try:
        with store.read_snapshot(dependencies=dependencies) as snapshot:
            item = snapshot.store.musical_profiles.source_input(key, batch, observation_ids=observation_ids)
            if item is None:
                return False
            source, pending, ids = item
            profile = profiles.source_profile(source)
            payload = profiles.canonical(profile)
            if len(payload.encode()) > 512 * 1024:
                raise WorkBudgetExceeded('Source profile output exceeds byte budget')
            check_stopping()
            if snapshot.publish_ordered(lambda: store.musical_profiles.publish_source(
                    key, pending, ids, payload, profiles.VERSION,
                    profiles.digest(profile), now, batch)):
                return True
    except WorkBudgetExceeded:
        pass
    store.musical_profiles.rotate(key)
    store.maintenance_diagnostics.record('deferred', reasons=('source-profile-budget-or-stale',),
                                         phase='rec: musical profiles')
    return False


def load_inputs(reader, rows):
    """The source/override/truncation inputs `recordings` needs, by recording key."""
    return reader.musical_profiles.recording_inputs([row[0] for row in rows])


def load_masked_inputs(reader, rows):
    """The source/override/truncation inputs `masked_recordings` needs, by recording key."""
    return reader.musical_profiles.recording_inputs([row[0] for row in rows],
                                                   source_scopes=('artist', 'release'))


def recordings(reader, rows, *, inputs=None):
    if inputs is None:
        inputs = load_inputs(reader, rows)
    result = []
    for key, _revision, raw, _digest, _different in rows:
        check_stopping()
        content = json.loads(raw)
        features = content['library'] or content['discovered']
        if features is None:
            result.append((key, None, profiles.VERSION, None))
            continue
        sources, overrides, truncated = inputs[key]
        # Old versions remain readable until replacement, but cannot feed a new resolver.
        sources = [source for source in sources if source['version'] == profiles.VERSION]
        genre = overrides.get('genre') if 'genre' in overrides else features.get('genre')
        profile = profiles.resolve(sources, selected=features.get('audio'), overrides=overrides, genre=genre)
        profile['sources_truncated'] = truncated
        result.append((key, profiles.canonical(profile), profiles.VERSION, profiles.digest(profile)))
    return result


def masked_recordings(reader, rows, *, inputs=None):
    """Prepare metadata conditioning with own observations removed before resolve.

    Source scope admission must precede the repository's source/byte limits.
    Prepared target profiles cannot be unmasked, so always start from inputs.
    """
    if inputs is None:
        inputs = load_masked_inputs(reader, rows)
    masked, allowed = [], {}
    for key, revision, raw, digest, different in rows:
        content = json.loads(raw)
        for features in content.values():
            if features is not None:
                features['audio'] = {}
        sources, overrides, truncated = inputs[key]
        if any(source['scope'] not in ('artist', 'release') for source in sources):
            raise ValueError('Masked metadata requires source admission before source limits')
        allowed[key] = (sources, {name: value for name, value in overrides.items() if name == 'genre'}, truncated)
        masked.append((key, revision, json.dumps(content), digest, different))
    return recordings(None, masked, inputs=allowed)
