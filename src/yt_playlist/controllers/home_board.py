"""Assemble the Home board and record the choices offered to the listener."""
from yt_playlist.rec import home_vibe


class HomeBoard:
    def __init__(self, *, load, cards, starter,
                 present, audit, tuning, remaining, fingerprint, modes, current_vibe,
                 discovery, artist_images, busy, pool, refresh, ledger, parameter, close, epoch, start_pool):
        self.load, self.calculate, self.starter = load, cards, starter
        self.present, self.audit = present, audit
        self.tuning, self.remaining, self.fingerprint = tuning, remaining, fingerprint
        self.modes, self.current_vibe = modes, current_vibe
        self.discovery, self.artist_images, self.busy = discovery, artist_images, busy
        self.pool, self.refresh = pool, refresh
        self.ledger, self.parameter, self.close, self.epoch, self.start_pool = ledger, parameter, close, epoch, start_pool

    def refresh_card(self, payload, now, card, path):
        proto = self.refresh(payload, now, card)
        if proto:
            self.audit([proto], now, path)
        return proto

    def refresh_all(self, now, opportunity=''):
        self.close(now, opportunity_id=opportunity)
        self.ledger.refresh_card('cards', max(1, self.parameter('erosion_view_cap')), now)

    def request_pool(self, kind, parameters, excluded, now):
        available = {row['family'] for row in (self.load(now) or {}).get('genre_options', [])}
        if any(len(parameters.get(key, [])) > 100 or any(value not in available for value in parameters.get(key, []))
               for key in ('include_genres', 'avoid_genres')):
            raise ValueError('Invalid genres')
        if kind == 'library_pool' and (parameters.get('lane') not in ('wheelhouse', 'explore', 'comfort', 'temporal')
                or parameters.get('epoch') != self.epoch()
                or not isinstance(excluded, list) or len(excluded) > 200
                or any(not isinstance(key, str) or len(key) > 1000 for key in excluded)):
            raise ValueError('Invalid library pool')
        parameters = {**parameters, **{key: sorted(set(parameters.get(key, [])))
                      for key in ('include_genres', 'avoid_genres')}}
        return self.start_pool(kind, parameters, excluded)

    def query(self, payload, now, *, changed=False, record=False, path='/', attempt=0, complete_only=False, **options):
        result = self.calculate(payload, now, record=record,
                                verify_vibe=path == '/home/vibe', **options)
        present = changed or (record and (not complete_only or len(result.protos) >= 4))
        result = self.starter(result, now)
        if present and result.protos:
            self.present(result, now)
        self.audit(result.protos, now, path)
        context = vars(result).copy()
        if (path in ('/', '/home/cards', '/home/feed') and not result.vibe and not result.genre_notice
                and result.slots and len(result.protos) < 4):
            # An incomplete ordinary board can gain a lane when its inputs arrive.
            context['prepare_attempt'] = min(10, attempt + 1)
            context['prepare_interval'] = min(30, 3 * 2 ** min(attempt, 4))
        return context

    def controls(self, payload, now, *, vibes_ready):
        return {'fingerprint': self.fingerprint(payload, now), 'tuning_active': self.tuning(),
                'vibe_presets': home_vibe.PRESETS, 'vibes_ready': vibes_ready, 'vibe': self.current_vibe(now)}

    def feed(self, payload, now, *, vibes_ready, lazy_tuning=False, **options):
        has_modes = self.modes()
        if lazy_tuning:
            families = (payload or {}).get('fingerprint', {}).get('breadth', {}).get('families', {})
            controls = {'lazy_tuning': True, 'show_tuning': bool(families or has_modes),
                        'tuning_active': self.tuning(), 'tuning_remaining': self.remaining(now),
                        'vibe': self.current_vibe(now)}
        else:
            controls = self.controls(payload, now, vibes_ready=vibes_ready)
        board = self.query(payload, now, **options)
        if board['protos']:
            controls['show_tuning'] = True
        return {**controls, 'has_modes': has_modes, **board}

    def discovery_query(self, payload, now):
        selection = self.discovery(payload, now)
        self.artist_images(selection, now)
        return {**selection, 'discovery_busy': self.busy()}
