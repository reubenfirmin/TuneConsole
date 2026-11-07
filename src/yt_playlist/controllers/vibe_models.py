"""Shared model validation, rebuilt receipts and bounded file retirement."""
from collections import OrderedDict
import hashlib

from yt_playlist.core import fao, vibe_model_files
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.evidence_ledger import canonical


def same_receipt(marker, value):
    return bool(marker and marker == (hashlib.sha256(canonical(value)).digest(), 'ready', value['generation']))


class VibeModels:
    def __init__(self, repository, files, provenance):
        self.repository, self.files, self.provenance = repository, files, provenance

    def stage(self, value):
        return self.files.stage(value)

    def read(self, generation):
        value = self.files.read(generation)
        inputs = {row['measurement']: row for row in value['inputs']}
        identities, found = list(inputs), set()
        for start in range(0, len(identities), 128):
            check_stopping()
            for row in self.repository.measurements(identities[start:start + 128]):
                item = inputs[row['id']]
                expected = (item['recording'], value['vibe'], item['score'], item['support'],
                            item.get('input_digest'), item.get('version'), item['source'])
                actual = (row['recording_id'], row['vibe'], row['score'], row['support'],
                          row['input_digest'], row['exact_version'], row['source'] + '/' + row['exact_version'])
                if expected != actual:
                    raise fao.CorruptFile('Shared vibe manifest differs from its original measurements')
                found.add(row['id'])
        if found != inputs.keys():
            raise fao.CorruptFile('Shared vibe manifest requires missing original measurements')
        return value

    def publish_receipts(self, values):
        """Rebuild exact compact receipts after validating all original owners."""
        loaded, generations, roots, receipts = OrderedDict(), [], set(), {}
        for pid, receipt, artists in values:
            if pid in receipts and receipts[pid] != receipt:
                raise ValueError('Rebuilt calculation changed its shared model receipt')
            receipts[pid] = receipt
            generation = receipt['generation']
            if generation not in loaded:
                loaded[generation] = self.read(generation)
                if len(loaded) > 8:
                    loaded.popitem(last=False)
            loaded.move_to_end(generation)
            model, facts = loaded[generation], self.repository.receipt_facts(pid, artists)
            prediction = facts['prediction']
            if (prediction is None or prediction[0] != model['vibe']
                    or prediction[1] in {row['recording'] for row in model['inputs']}):
                raise ValueError('Shared model does not match rebuilt prediction')
            if sorted(set(artists)) != receipt['artists']:
                raise ValueError('Shared model artist conditioning changed')
            if any(type(artist) is not int or artist not in facts['artists'] for artist in artists):
                raise ValueError('Unknown conditioning artist')
            if receipt != vibe_model_files.receipt(generation, artists, receipt['mean_variance'],
                    receipt['new_track_variance'], conditioning=receipt.get('conditioning')):
                raise ValueError('Invalid shared model receipt')
            if facts['generation'] is not None and facts['generation'] != generation:
                raise ValueError('Rebuilt calculation changed its shared model identity')
            if facts['marker'] and not same_receipt(facts['marker'], receipt):
                raise ValueError('Rebuilt calculation changed its shared model receipt')
            roots.update(vibe_model_files.measurement_ids(model))
            generations.append((pid, bytes.fromhex(generation)))
        staged, markers, _roots = self.provenance.stage_many(values)
        try:
            for publication in staged:
                publication.publish()
            self.repository.install_receipts(generations, sorted(roots), markers)
        finally:
            for publication in staged:
                publication.close()

    def collect(self, now, limit=16):
        if not 1 <= limit <= 128:
            raise ValueError('Shared vibe collection requires a bounded limit')
        self.repository.assert_idle()
        with self.files.collection() as work:
            if work is None:
                return 0
            removed = 0
            for generation in work.generations(limit):
                if not self.repository.referenced(generation) and work.created_at(generation) <= now - 30 * 86400:
                    work.remove(generation)
                    removed += 1
            work.save()
            return removed
