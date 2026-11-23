"""Assemble one Home batch from the current prepared inputs."""
from copy import deepcopy
from dataclasses import fields

from yt_playlist.rec.home_card_values import experiment_id


def menu_values(result):
    menu = {field.name: deepcopy(getattr(result, field.name)) for field in fields(result)
            if field.name not in ('choice_context', 'protos', 'slots')}
    menu['protos'] = deepcopy(result.protos)
    for proto in menu['protos']:
        proto['experiment_id'] = experiment_id(proto)
        proto['recipe'] = {**proto.get('recipe', {}), 'experiment_id': proto['experiment_id']}
    by_lane = {proto['lane']: proto for proto in menu['protos']}
    menu['slots'] = [{**deepcopy({key: value for key, value in slot.items() if key != 'proto'}),
                      'proto': by_lane[slot['proto']['lane']] if slot['proto'] else slot['proto']}
                     for slot in result.slots]
    return menu


class HomePreparation:
    def __init__(self, *, calculate, freeze, is_complete, has_complete, encode, publish, check, elapsed, deferred,
                 max_menus, budget, max_bytes, schema):
        self.calculate, self.freeze, self.is_complete, self.has_complete = calculate, freeze, is_complete, has_complete
        self.encode, self.publish, self.check = encode, publish, check
        self.elapsed, self.deferred = elapsed, deferred
        self.max_menus, self.budget, self.max_bytes, self.schema = max_menus, budget, max_bytes, schema

    def prepare(self, *, cursor, adaptive, now, tuning, generation, options, fingerprint, collections):
        menus = []
        collections = dict(collections)
        choice_context = collections['choice_context']
        for index in range(self.max_menus if adaptive else 1):
            self.check()
            result = self.calculate(cursor + index)
            if not choice_context and result.choice_context:
                choice_context = result.choice_context
            menus.append(menu_values(result))
            if self.elapsed() >= self.budget * .5:
                break
        complete = [menu for menu in menus if self.is_complete(menu)]
        if complete:
            menus = complete
        elif self.has_complete():
            self.deferred('incomplete_menus')
            return None
        # Freeze after assembly so this worker's evidence writes do not
        # invalidate the committed read cache used while preparing the menus.
        if choice_context:
            frozen = self.freeze(choice_context)
            if 'group_generation' in choice_context:
                frozen['group_generation'] = choice_context['group_generation']
            collections['choice_context'] = frozen
        payload = {'schema': self.schema, 'built_at': now, 'tuning': tuning, 'generation': generation,
            'menus': menus, 'rotation_epoch': cursor, 'genre_options': options, 'modes': [],
            'group_policy': 'adaptive' if adaptive else 'legacy', 'fingerprint': fingerprint, **collections}
        encoded = self.encode(payload)
        if len(encoded.encode()) > self.max_bytes:
            raise ValueError('Prepared Home collections exceeded their size budget')
        self.check()
        return payload if self.publish(encoded, now) else None
