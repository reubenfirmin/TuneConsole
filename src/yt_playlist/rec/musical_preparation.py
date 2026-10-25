"""Small slices on the existing preparation worker, with last-good publication."""
import json

from yt_playlist.core.activity import work_budget, WorkBudgetExceeded, check_stopping
from yt_playlist.rec import musical_profiles as profiles


def due(store):
    return bool(store.musical_profiles.pending() or any(store.musical_profiles.stale(profiles.VERSION, 1)))


def source_step(store, now, *, batch=32, seconds=2):
    store.musical_profiles.queue_stale(profiles.VERSION, batch)
    key = store.musical_profiles.pending()
    if not key:
        return False
    dependencies = {(table, 'key:' + key) for table in (
        'rec_musical_pending', 'rec_musical_sources', 'rec_musical_links', 'rec_musical_source_profiles')}
    try:
        with work_budget(seconds), store.read_snapshot(dependencies=dependencies) as snapshot:
            item = snapshot.store.musical_profiles.source_input(key, batch)
            if item is None:
                return False
            source, pending, ids = item
            profile = profiles.source_profile(source)
            payload = profiles.canonical(profile)
            if len(payload.encode()) > 512 * 1024:
                raise WorkBudgetExceeded('Source profile output exceeds byte budget')
            check_stopping()
            if snapshot.publish(lambda: store.musical_profiles.publish_source(
                    key, pending, ids, payload, profiles.VERSION,
                    profiles.digest(profile), now, batch)):
                return True
    except WorkBudgetExceeded:
        pass
    store.musical_profiles.rotate(key)
    store.maintenance_diagnostics.record('deferred', reasons=('source-profile-budget-or-stale',),
                                         phase='rec: musical profiles')
    return False


def recordings(reader, rows):
    inputs = reader.musical_profiles.recording_inputs([row[0] for row in rows])
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
