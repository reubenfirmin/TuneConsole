"""Capture coherent model facts and bind the model-preparation publication owners."""
from yt_playlist.controllers.model_preparation import ModelPreparation
from yt_playlist.rec import artist_model, embedding_training, embedding_vectors, model_calculations as model_refresh, rec_params


def capture(reader):
    settings = reader.settings
    baskets = reader.rec_evidence.rec_baskets()
    content = embedding_vectors.content_inputs(reader)
    discoveries = reader.discovery.get_discovered_tracks()
    axes = reader.genres.feature_axes([*(value[0] for value in content.content.values()),
                                      *(row.get('genre') for row in discoveries)])
    refs = reader.artists.track_keys(library=True)
    profiles = artist_model.profiles_from_tracks(content.content, content.audio, refs, axes)
    for artist, profile in artist_model._discovered_artist_profiles(reader).items():
        profiles.setdefault(artist, profile)
    return model_refresh.PreparationInputs(
        method=settings.get_setting('rec_embed_method') or 'svd',
        dim=int(settings.get_setting('rec_dim') or embedding_training.DIM),
        baskets=embedding_training.canonical_baskets(baskets),
        weights=(embedding_training.cooc_weights(reader.rec_query.play_counts())
                 if settings.get_setting('rec_cooc_weighting') == '1' else None),
        fingerprints={name: settings.get_setting(name) for name in
                      ('rec_fit_inputs', 'rec_artist_fit_inputs', 'rec_mode_fit_inputs')},
        content=content, previous=reader.rec_model.content_generation(), discoveries=discoveries, axes=axes,
        artist_profiles=profiles, artist_baskets=artist_model.artist_baskets_from_tracks(baskets, refs),
        mode_params={name: rec_params.get_param(reader, name) for name in
                     ('modes_k', 'modes_min_members', 'modes_match_threshold')},
        modes=reader.modes.list_modes(active_only=True), next_mode_id=reader.modes.next_mode_id(),
        genres=reader.modes.genres_for(content.keys), track_count=reader.rec_model.rec_vectors_count())


def preparation(store):
    return ModelPreparation(snapshot=store.read_snapshot, refresh=store.rec_refresh,
        model=store.rec_model, settings=store.settings, modes=store.modes,
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
