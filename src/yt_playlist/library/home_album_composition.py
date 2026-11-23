"""Bind album acquisition and evidence to explicit menu command capabilities."""
from yt_playlist.controllers.album_menu import AlbumMenu, AlbumNotesPreparation
from yt_playlist.rec import album_choices, album_notes, album_tuning, choice_evidence
from yt_playlist.rec.home_cards import _epoch


def album_menu(store, writer, *, now_playing, schedule, worker_available, logger):
    return AlbumMenu(writer.rec_proposals, writer.album_notes, writer.rec_ledger, store.genres, writer.settings,
        prepared=lambda now: album_choices.load(store, now, schedule),
        current=lambda: album_choices.current_browse_id(store, now_playing()),
        epoch=lambda: _epoch(store, 'album_choices'), tuning=lambda now: album_tuning.state(store, now),
        defaults=lambda: album_tuning.defaults(store), excluded=lambda epoch: album_choices.excluded_from_refresh(store, epoch),
        select=lambda *args, **kw: album_choices.selection(store, *args, **kw),
        listening=store.collection.album_listening_summary, read_notes=lambda album: album_notes.read(store, album),
        present=lambda options, now, **kw: choice_evidence.present(writer, 'albums', options, now, **kw),
        close=lambda now, **kw: choice_evidence.close_window(writer, 'albums', now, **kw),
        opportunity=lambda oid: choice_evidence.opportunity(store, oid), schedule=schedule,
        worker_available=worker_available, logger=logger)


def note_preparation(store, *, now_playing, clients, now, stage):
    from yt_playlist.core.accounts import catalog_client
    return AlbumNotesPreparation(tuning=lambda at: album_tuning.state(store, at),
        epoch=lambda: _epoch(store, 'album_choices'),
        excluded=lambda epoch: album_choices.excluded_from_refresh(store, epoch),
        current=lambda: album_choices.current_browse_id(store, now_playing()),
        select=lambda *args, **kw: album_choices.selection(store, *args, **kw),
        prepare=lambda menu, at: stage('Album sleeve notes', album_notes.prepare, store, at, menu,
                                      client=catalog_client(clients()), now_fn=now))
