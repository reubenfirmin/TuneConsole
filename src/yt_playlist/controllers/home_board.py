"""Home board query and publication effects over explicit preparation capabilities."""
from yt_playlist.rec import home_prepared, home_vibe


class HomeBoard:
    def __init__(self, *, load, live, expire, cards, starter, remember, schedule, vibe,
                 present, audit, tuning, remaining, fingerprint, modes, current_vibe,
                 discovery, artist_images, busy, pool, refresh, ledger, parameter, close, epoch, start_pool):
        self.load, self.live, self.expire, self.calculate = load, live, expire, cards
        self.starter, self.remember, self.schedule = starter, remember, schedule
        self.vibe, self.present, self.audit = vibe, present, audit
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

    def query(self, payload, now, *, live=False, record=False, path='/', attempt=0, **options):
        if live:
            self.expire(now)
            payload, result, tuning = self.live(now, options)
            self.remember(payload, result, now, tuning=tuning)
            self.schedule()
        else:
            result = self.calculate(payload, now, record=record)
        result = self.starter(result, now)
        vibe = self.vibe()
        context = board_values(result, vibe, attempt, live)
        if (live or record) and result.protos:
            self.present(result, payload, now)
        self.audit(result.protos, now, path)
        return context

    def controls(self, payload, now, *, vibes_ready):
        return {'fingerprint': self.fingerprint(payload, now), 'tuning_active': self.tuning(),
                'vibe_presets': home_vibe.PRESETS, 'vibes_ready': vibes_ready, 'vibe': self.current_vibe(now)}

    def feed(self, payload, now, *, vibes_ready, **options):
        return {**self.controls(payload, now, vibes_ready=vibes_ready), 'has_modes': self.modes(),
                **self.query(payload, now, **options)}

    def discovery_query(self, payload, now):
        selection = self.discovery(payload, now)
        self.artist_images(selection, now)
        return {**selection, 'discovery_busy': self.busy()}

    def pool_query(self, job, payload, now, path):
        result = self.pool(job, now, path)
        if result.proto:
            menu = self.calculate(payload, now, record=False)
            lane = result.proto['lane']
            menu.protos = [p for p in menu.protos if p['lane'] != lane] + [result.proto]
            menu.slots = [{'proto': result.proto, 'pool': None} if
                          (slot['proto']['lane'] if slot['proto'] else slot['pool']['lane']) == lane else slot
                          for slot in menu.slots] or [{'proto': result.proto, 'pool': None}]
            self.remember(payload, menu, now)
        return result


def board_values(result, vibe_request, attempt=0, live=False):
    context = vars(result).copy()
    context['vibe_request'] = vibe_request
    pending = bool(vibe_request and vibe_request['phase'] in ('pending', 'waiting'))
    if pending:
        context.update(preparing=True, preference_notice=None)
    waiting = result.preparing or (not result.protos and bool(result.slots))
    context['prepare_attempt'], context['prepare_interval'] = home_prepared.polling_schedule(
        min(10, max(0, attempt)), live=live, waiting=waiting, vibe_pending=pending)
    return context
