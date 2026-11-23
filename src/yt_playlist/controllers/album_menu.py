"""Album menu, tuning, note demand and choice evidence workflows."""
from yt_playlist.rec import album_tuning


def familiarity(summary):
    if not summary['complete'] or not summary['tracks']:
        return {'level': 'partial' if summary['plays'] else 'unknown',
                'label': 'Some listening recorded; album coverage is incomplete.' if summary['plays']
                         else 'Listening history unavailable for this album.'}
    plays = summary['plays']
    average = plays / summary['tracks']
    level, label = ('unplayed', 'No recorded track plays.') if not plays else (
        ('light', 'Lightly played.') if average < 1 else
        ('familiar', 'Familiar.') if average < 3 else ('frequent', 'Frequently played.'))
    return {'level': level, 'label': label}


class AlbumMenu:
    def __init__(self, proposals, notes, ledger, genres, settings, *, prepared, current, epoch,
                 tuning, defaults, excluded, select, listening, read_notes, present, close,
                 opportunity, schedule, worker_available, logger):
        self.proposals, self.notes, self.ledger, self.genres, self.settings = proposals, notes, ledger, genres, settings
        self.prepared, self.current, self.epoch, self.tuning = prepared, current, epoch, tuning
        self.defaults, self.excluded, self.select, self.listening = defaults, excluded, select, listening
        self.read_notes, self.present, self.close, self.opportunity = read_notes, present, close, opportunity
        self.schedule, self.worker_available, self.logger = schedule, worker_available, logger

    def selection(self, now):
        prepared = self.prepared(now)
        payload, epoch, tuning = prepared or {}, self.epoch(), self.tuning(now)
        pool, items, policy = select_menu(payload, now, epoch, tuning, self.excluded(epoch), self.current(), self.select)
        return prepared, payload, epoch, tuning, pool, items, policy

    def query(self, now):
        prepared, payload, epoch, tuning, pool, items, policy = self.selection(now)
        defaults = self.defaults()
        policy = {**policy, 'temporary_until': now + tuning['remaining'] if tuning['remaining'] else None,
                  'standing_defaults': defaults}
        options = [{**album, 'id': album['browse_id'], 'position': index + 1} for index, album in enumerate(items)]
        oid = None
        if options:
            try:
                oid = self.present(options, now, context={**payload.get('context', {}), 'album_menu': policy}, window=epoch)
            except Exception:
                self.logger.warning('Album choice capture failed', exc_info=True)
        self.notes.request(items, now)
        pending = self.proposals.get_proposals('album_note_requests') or {}
        if self.worker_available and pending:
            self.schedule()
        listening = self.listening(album['browse_id'] for album in items)
        items = [{**album, 'notes': self.read_notes(album), 'familiarity': familiarity(listening[album['browse_id']]),
                  'notes_pending': album['browse_id'] in pending and self.worker_available} for album in items]
        genres = {axis[0] for axis in self.genres.feature_axes(album.get('genre') for album in pool).values()} - {''}
        genres = sorted(genres | set(tuning['genres']))
        return {'albums': items, 'opportunity_id': oid, 'preparing': prepared is None,
                'filtered': bool(tuning['genres']), 'tuning': tuning, 'tuning_axes': album_tuning.AXES,
                'tuning_defaults': defaults,
                'genres': [{'key': genre, 'label': genre.removeprefix('other:').title()} for genre in genres]}

    def rotate(self, now, previous=''):
        held = self.close(now, opportunity_id=previous)
        items = held['options'] if held else self.selection(now)[5]
        self.ledger.refresh_card('album_choices', 1, now)
        self.proposals.put_proposals('album_menu_refresh',
            {'epoch': self.epoch(), 'exclude': [item['browse_id'] for item in items]}, now)

    def tune(self, now, values, genres, *, reset=False, previous=''):
        if reset:
            self.settings.delete_setting(album_tuning.SETTING)
        else:
            known = set(self.genres.taxonomy()['families'])
            payload = self.prepared(now) or {}
            known.update(axis[0] for axis in self.genres.feature_axes(a.get('genre') for a in payload.get('items', [])).values())
            if any(genre not in known for genre in genres):
                raise ValueError('Unknown album genre')
            self.settings.set_setting(album_tuning.SETTING, album_tuning.encode(now, values, genres))
        self.rotate(now, previous)

    def note(self, browse, now, opportunity='', attempt=0):
        albums = (self.prepared(now) or {}).get('items', [])
        if opportunity and len(opportunity) <= 64:
            held = self.opportunity(opportunity)
            if held and held.get('format') == 'albums':
                albums = held.get('options', []) + albums
        album = next((row for row in albums if row.get('browse_id') == browse), None)
        if album is None:
            return None
        self.notes.request([album], now, visible=True)
        if self.worker_available:
            self.schedule()
        pending = browse in (self.proposals.get_proposals('album_note_requests') or {})
        return {'a': album, 'notes': self.read_notes(album), 'opportunity_id': opportunity, 'notes_scene': True,
                'notes_pending': pending and self.worker_available and attempt < 80,
                'notes_attempt': max(0, attempt) + 1}


def select_menu(payload, now, epoch, tuning, excluded, current, select):
    return select(payload.get('items', []), now, tuning['values'], epoch, tuning['genres'],
                  exclude=excluded, current=current)


class AlbumNotesPreparation:
    def __init__(self, *, tuning, epoch, excluded, current, select, prepare):
        self.tuning, self.epoch, self.excluded = tuning, epoch, excluded
        self.current, self.select, self.prepare_notes = current, select, prepare

    def prepare(self, payload, now):
        tuning, epoch = self.tuning(now), self.epoch()
        _, menu, _ = select_menu(payload or {}, now, epoch, tuning,
                                 self.excluded(epoch), self.current(), self.select)
        return self.prepare_notes(menu, now)
