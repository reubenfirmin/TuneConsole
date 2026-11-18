"""Coherent Home input capture and bindings for the board command owner."""
from yt_playlist.controllers.home_board import HomeBoard
from yt_playlist.controllers.home_tuning import tuning_active
from yt_playlist.rec import choice_evidence, home_cards, home_prepared, home_vibe, mix_tuning, rec_params
from yt_playlist.rec.home_cards import _epoch


def board(store, writer, *, jobs, schedule, vibe_requests, worker, logger, display_only=False):
    service = home_cards.HomeCards(writer, jobs, logger)

    def live(now, options):
        with store.read_view() as reader:
            payload = home_prepared.peek(reader, now, schedule)
            calculation = home_cards.HomeCards(reader, jobs, logger, genre_options=(payload or {}).get('genre_options'))
            result = calculation.cards(home_cards.CardsRequest(now, record=False, **options))
            tuning = home_prepared.tuning_key(reader, now)
        return payload, result, tuning

    def active():
        return tuning_active(store.rec_model.get_leans(), store.rec_model.hidden_facets(),
            store.settings.get_setting(home_vibe.SETTING), rec_params.get_param(store, 'breadth_bias'),
            store.rec_proposals.get_proposals('home_genre_request') or {}, _epoch(store, 'cards'))

    def images(selection, now):
        ids = [a['artist_id'] for a in selection.get('discovery_artists', []) if not a.get('thumbnail')]
        if worker and ids:
            worker.trigger_artist_images([a['artist_id'] for a in store.discovery.artist_images_due(ids, now)])

    commands = HomeBoard(load=lambda now: home_prepared.load(store, now, schedule, display_only=display_only),
        live=live, expire=lambda now: mix_tuning.expire(store, now),
        cards=lambda payload, now, **kw: home_prepared.cards(store, payload, now, service, schedule, writer=writer, **kw),
        starter=lambda result, now: home_prepared.with_starter_fallback(store, result, now),
        remember=lambda *args, **kw: home_prepared.remember(writer, *args, **kw), schedule=schedule,
        vibe=lambda: vibe_requests.state() if vibe_requests else None,
        present=lambda *args: home_prepared.present_mix_choices(writer, *args, logger=logger),
        audit=lambda protos, now, path: service.audit(protos, now, path, reader=store),
        tuning=active, remaining=lambda now: mix_tuning.remaining(store, now),
        fingerprint=lambda payload, now: home_prepared.fingerprint(store, payload, now),
        modes=lambda: bool(store.modes.list_summaries(active_only=True)),
        current_vibe=lambda now: home_vibe.current(store, now),
        discovery=lambda payload, now: home_prepared.discovery(store, payload, now),
        artist_images=images, busy=lambda: bool(worker and worker.discovery_busy),
        pool=service.pool,
        refresh=lambda payload, now, card: home_prepared.refresh_card_proto(writer, payload, now, service, schedule, card),
        ledger=writer.rec_ledger, parameter=lambda name: rec_params.get_param(store, name),
        close=lambda now, **kw: choice_evidence.close_window(writer, 'mixes', now, **kw),
        epoch=lambda: _epoch(store, 'cards'), start_pool=service.start_pool)
    return commands, service
