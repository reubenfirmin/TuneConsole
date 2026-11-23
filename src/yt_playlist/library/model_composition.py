"""Capture coherent model facts and bind the model-preparation publication owners."""
from yt_playlist.controllers.model_preparation import ModelPreparation
from yt_playlist.rec import artist_model, embedding_training, embedding_vectors, model_calculations as model_refresh


def capture(reader):
    settings = reader.settings
    baskets = reader.rec_evidence.rec_baskets()
    content, discoveries, character = embedding_vectors.capture_content_inputs(
        reader, reader.discovery.get_discovered_tracks())
    axes = reader.genres.feature_axes([*(value[0] for value in content.content.values()),
                                      *(row.get('genre') for row in discoveries)])
    refs = reader.artists.track_keys(library=True)
    profiles = artist_model.profiles_from_recordings(reader, content, discoveries, character, axes)
    for artist, profile in artist_model._discovered_artist_profiles(reader).items():
        if artist not in profiles:
            profiles[artist] = profile
        elif not profiles[artist]['families']:
            profiles[artist].update(families=profile['families'], subs=profile['subs'])
    return model_refresh.PreparationInputs(
        method=settings.get_setting('rec_embed_method') or 'svd',
        dim=int(settings.get_setting('rec_dim') or embedding_training.DIM),
        baskets=embedding_training.canonical_baskets(baskets),
        weights=(embedding_training.cooc_weights(reader.rec_query.play_counts())
                 if settings.get_setting('rec_cooc_weighting') == '1' else None),
        recording_fit=reader.rec_model.recording_state().fit_fingerprint,
        artist_fit=reader.rec_model.artist_state().fit_fingerprint,
        content=content, previous=reader.rec_model.content_generation(), discoveries=discoveries, axes=axes,
        artist_profiles=profiles, artist_baskets=artist_model.artist_baskets_from_tracks(baskets, refs),
        track_count=reader.rec_model.rec_vectors_count())


def preparation(store):
    return ModelPreparation(snapshot=store.read_snapshot, refresh=store.rec_refresh,
        model=store.rec_model,
        prepare=lambda reader, now, **options: model_refresh.calculate(capture(reader), now, **options))


def followup(store):
    from yt_playlist.controllers.model_followup import ModelFollowup
    return ModelFollowup(store.model_selection, store.settings, store.history, store.rec_evidence,
        forecast=lambda previous, selected, now: forecast(store, previous, selected, now))


def forecast(store, previous, selected, now):
    from yt_playlist.rec.model_forecasts import forecast as calculate
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        baskets = reader.rec_evidence.rec_baskets()
        weights = (embedding_training.cooc_weights(reader.rec_query.play_counts())
                   if reader.settings.get_setting('rec_cooc_weighting') == '1' else None)
        context = reader.history.history_keys_before(now) | reader.model_selection.selection_play_keys(0, now)
    return calculate(baskets, previous, selected, context, weights=weights)
