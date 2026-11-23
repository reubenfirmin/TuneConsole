"""Mode preparation uses existing guarded SQL/file publication owners."""
from yt_playlist.controllers.mode_preparation import ModePreparation
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.library import mix_listening
from yt_playlist.rec import candidate_refresh, measurement, mode_inputs as mode_surfaces


def scheduled(store, now):
    from yt_playlist.rec import recommendation_retirement
    with store._lock:
        if getattr(store, '_candidate_preparation_active', False) or not candidate_refresh.check_due(store, now):
            return None
        store._candidate_preparation_active = True
    try:
        recommendation_retirement.drain(store, now)
        with store._lock:
            candidate_refresh.checked(store, now)
        return preparation(store).prepare(now)
    finally:
        with store._lock:
            store._candidate_preparation_active = False


def preparation(store):
    def resume():
        with store._lock:
            deferred = store.__dict__.pop('_deferred_bundles', None)
        if deferred is None:
            return False, None
        published = deferred.publish(store)
        return True, deferred.payload if published is not None else None

    def capture(reader, now):
        with column_group_reads(reader, 'recordings', 'catalog'):
            policies = measurement.PoolPolicies(reader, now)
            payload, fresh = mode_surfaces._calculate_bundles(reader, now, policies)
            settings = candidate_refresh.prepared_settings(reader, now)
        return payload, fresh, policies, settings

    def maintain(now):
        measurement.maintain_storage(store, now)
        mix_listening.prepare(store, now)

    return ModePreparation(maintain=maintain,
        resume=resume, snapshot=store.read_snapshot, capture=capture,
        publish=lambda snapshot, *args: mode_surfaces._publish_typed_bundles(store, snapshot, *args))
