"""Complete Home batch preparation, retaining usable generations during catch-up."""
from dataclasses import asdict

from yt_playlist.rec.home_card_values import experiment_id


def menu_order(offset, mode_ids):
    start = (offset // 3 + (offset % 3) * 4) % len(mode_ids) if mode_ids else 0
    return tuple(mode_ids[start:] + mode_ids[:start]) if mode_ids else None


def menu_values(result):
    menu = asdict(result)
    menu.pop('choice_context', None)
    for proto in menu['protos']:
        proto['experiment_id'] = experiment_id(proto)
        proto['recipe'] = {**proto.get('recipe', {}), 'experiment_id': proto['experiment_id']}
    by_lane = {proto['lane']: proto for proto in menu['protos']}
    for slot in menu['slots']:
        if slot['proto']:
            slot['proto'] = by_lane[slot['proto']['lane']]
    return menu


class HomePreparation:
    def __init__(self, *, calculate, freeze, has_complete, encode, publish, check, elapsed, deferred,
                 max_menus, budget, max_bytes, schema):
        self.calculate, self.freeze, self.has_complete = calculate, freeze, has_complete
        self.encode, self.publish, self.check = encode, publish, check
        self.elapsed, self.deferred = elapsed, deferred
        self.max_menus, self.budget, self.max_bytes, self.schema = max_menus, budget, max_bytes, schema

    def prepare(self, *, previous, reusable, mode_ids, modes, adaptive, now, tuning, generation,
                options, fingerprint, collections, play_exposure):
        menus = {menu['epoch']: menu for menu in previous.get('menus', [])} if reusable else {}
        cursor = previous.get('next_menu', 0) if reusable else 0
        collections = dict(collections)
        for index in range(self.max_menus if mode_ids or adaptive else 1):
            self.check()
            offset = (cursor + index) % self.max_menus
            result = self.calculate(offset, menu_order(offset, mode_ids))
            if result.choice_context and not collections['choice_context']:
                collections['choice_context'] = {**self.freeze(result.choice_context),
                    'group_generation': result.choice_context['group_generation']}
            menus[offset] = menu_values(result)
            if self.elapsed() >= self.budget * .5:
                break
        complete = {key: menu for key, menu in menus.items() if len(menu['protos']) >= 4}
        if len(complete) < len(menus) and self.has_complete(previous, now):
            if not complete:
                self.deferred('incomplete_menus')
                return None
            menus = complete
        payload = {'schema': self.schema, 'built_at': now, 'tuning': tuning, 'generation': generation,
            'next_menu': (offset + 1) % self.max_menus, 'menus': [menus[key] for key in sorted(menus)],
            'genre_options': options, 'modes': [{**mode, 'centroid': list(map(float, mode['centroid']))} for mode in modes],
            'group_policy': 'adaptive' if adaptive else 'legacy', 'play_exposure': play_exposure,
            'fingerprint': fingerprint, **collections}
        encoded = self.encode(payload)
        if len(encoded.encode()) > self.max_bytes:
            raise ValueError('Prepared Home collections exceeded their size budget')
        self.check()
        return payload if self.publish(encoded, now) else None
