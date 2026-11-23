"""Coherent Home input capture and bindings for the board command owner."""
from yt_playlist.controllers.home_board import HomeBoard
from yt_playlist.controllers.home_tuning import tuning_active
from yt_playlist.rec import choice_evidence, home_cards, home_prepared, home_vibe, mix_tuning, ordering, rec_params, taste_analysis
from yt_playlist.rec.eligibility import comfort_tracks, home_exclusions
from yt_playlist.rec.home_cards import _epoch


def board(store, writer, *, jobs, worker, logger, defer=None):
    service = home_cards.HomeCards(writer, jobs, logger)

    def active():
        return tuning_active(store.rec_model.get_leans(), store.rec_model.hidden_facets(),
            store.settings.get_setting(home_vibe.SETTING), rec_params.get_param(store, 'breadth_bias'),
            store.rec_proposals.get_proposals('home_genre_request') or {}, _epoch(store, 'cards'))

    def images(selection, now):
        ids = [a['artist_id'] for a in selection.get('discovery_artists', []) if not a.get('thumbnail')]
        if worker and ids:
            worker.trigger_artist_images([a['artist_id'] for a in store.discovery.artist_images_due(ids, now)])

    def load(now):
        summary = taste_analysis.library_summary(store)
        return {'genre_options': summary['genre_options'], 'fingerprint': {
            'breadth': summary['breadth'], 'eras': summary['eras'], 'transient': {}}}

    def select(payload, request):
        with store.read_view(pinned=True) as reader:
            inputs = payload if payload is not None else load(request.now)
            calculation = home_cards.HomeCards.live(reader, jobs, logger,
                {**inputs['fingerprint'], 'genre_options': inputs['genre_options']})
            return calculation.cards(request)

    def ineligible(result, now):
        keys = {ordering._field(track, 'key') for proto in result.protos for track in proto['tracks']}
        comfort = {ordering._field(track, 'key') for proto in result.protos
                   if proto['lane'] == 'comfort' for track in proto['tracks']}
        with store.read_view(pinned=True) as reader:
            blocked = home_exclusions(reader, keys, now)
            rested = {track['key'] for track in comfort_tracks(reader, [{'key': key} for key in comfort], now)}
        return blocked, comfort - rested

    def calculate(payload, now, *, record=False, verify_vibe=False, **options):
        mix_tuning.expire(store, now)
        request = home_cards.CardsRequest(now, record=False, **options)
        result = select(payload, request)
        if verify_vibe and result.vibe and result.protos:
            # The selection snapshot can predate a confirmed rating or play.
            # Check only the offered recordings before attributing the menu.
            blocked, heard = ineligible(result, now)
            if blocked or heard:
                result = select(payload, request)
                blocked, heard = ineligible(result, now)
                if blocked or heard:
                    # Keep the usable songs in order if feedback changed again.
                    for proto in result.protos:
                        excluded = blocked | heard if proto['lane'] == 'comfort' else blocked
                        proto['tracks'] = [track for track in proto['tracks']
                                           if ordering._field(track, 'key') not in excluded]
                    result.protos = [proto for proto in result.protos if proto['tracks']]
                    result.slots = [slot for slot in result.slots if not slot['proto'] or slot['proto']['tracks']]
        if record and any(proto.get('mode_id') is not None for proto in result.protos):
            if defer is None:
                service.record_modes(result.protos, result.epoch, now)
            else:
                defer('record menu modes', service.record_modes, result.protos, result.epoch, now)
        return result

    def present(result, now):
        home_prepared.present_mix_choices(writer, result, {'choice_context': {}}, now,
                                          reader=store, logger=logger, defer=defer)

    commands = HomeBoard(load=load,
        cards=calculate,
        starter=lambda result, now: home_prepared.with_starter_fallback(store, result, now),
        present=present,
        audit=lambda protos, now, path: service.audit(protos, now, path, reader=store, defer=defer),
        tuning=active, remaining=lambda now: mix_tuning.remaining(store, now),
        fingerprint=lambda payload, now: home_prepared.fingerprint(store, payload, now),
        modes=store.taste_groups.has_adaptive_head,
        current_vibe=lambda now: home_vibe.current(store, now),
        discovery=lambda payload, now: home_prepared.discovery(store, payload, now),
        artist_images=images, busy=lambda: bool(worker and worker.discovery_busy),
        pool=service.pool,
        refresh=lambda payload, now, card: home_prepared.refresh_card_proto(writer, payload, now, service, card, calculate),
        ledger=writer.rec_ledger, parameter=lambda name: rec_params.get_param(store, name),
        close=lambda now, **kw: choice_evidence.close_window(writer, 'mixes', now, **kw),
        epoch=lambda: _epoch(store, 'cards'), start_pool=service.start_pool)
    return commands, service
