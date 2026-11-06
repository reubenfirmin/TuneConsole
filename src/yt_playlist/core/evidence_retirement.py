"""Durable reference-retirement intent across interrupted evidence unlinks.

Only identities are retained here. Each turn derives deletions again from live
evidence and current protection; this is never permission to replay stale paths.
Unlike the inventory cache, this record must survive until reconciliation ends.
"""
import json

from . import fao
from .evidence_contract import canonical

BATCH = 32


class Retirements:
    def __init__(self, directory):
        self.path = directory / '.retention-retirements'
        try:
            value = json.loads(fao.read_bytes(self.path))
        except FileNotFoundError:
            value = {'schema': 1, 'offers': [], 'checkpoints': []}
        if (not isinstance(value, dict) or value.get('schema') != 1
                or any(not isinstance(value.get(key), list)
                       or any(not isinstance(identity, str) for identity in value[key])
                       for key in ('offers', 'checkpoints'))):
            raise ValueError('Invalid evidence retirement intent')
        self.offers = set(value['offers'])
        self.checkpoints = set(value['checkpoints'])

    def save(self):
        fao.write_bytes(self.path, canonical({'schema': 1, 'offers': sorted(self.offers),
                                             'checkpoints': sorted(self.checkpoints)}))

    def reconcile(self, offers, checkpoints, callback):
        # Commit small prefixes. A cutoff during/after a SQL callback repeats at
        # most this idempotent batch; it cannot lose the remaining identities.
        offers, checkpoints = sorted(offers), sorted(checkpoints)
        while offers or checkpoints:
            batch_offers, batch_checkpoints = offers[:BATCH], checkpoints[:BATCH]
            if callback is not None:
                callback(batch_offers, batch_checkpoints)
            self.offers.difference_update(batch_offers)
            self.checkpoints.difference_update(batch_checkpoints)
            self.save()
            del offers[:BATCH]
            del checkpoints[:BATCH]
