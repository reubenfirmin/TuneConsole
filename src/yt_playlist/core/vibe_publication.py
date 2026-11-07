"""Prediction reuse, contributor admission and publication plans without I/O."""
from dataclasses import dataclass, field
import hashlib
import json

from . import fao, vibe_model_files
from .evidence_ledger import canonical


@dataclass
class PreparedVibes:
    measurements: list
    samples: list
    predictions: list
    publications: list
    deleted: list
    cohorts: set
    provenance: list = field(default_factory=list)
    staged: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    roots: list = field(default_factory=list)
    generations: list = field(default_factory=list)

    def close(self):
        for publication in self.staged:
            publication.close()


def _same_inputs(facts, pid, inputs):
    saved = facts.get(pid)
    if saved is None:
        return False
    fingerprint, state, _generation, legacy = saved
    if fingerprint is None:
        return set(legacy) == inputs
    if state != 'ready':
        raise fao.CorruptFile('Current calculation provenance expired while referenced')
    return fingerprint == hashlib.sha256(canonical(sorted(inputs))).digest()


def _same_receipt(facts, pid, receipt):
    saved = facts.get(pid)
    return bool(saved and saved[1] == 'ready'
                and saved[0] == hashlib.sha256(canonical(receipt)).digest()
                and saved[2] == receipt['generation'])


def prepare(tracks, resolved, touched, now, facts, genre_axes):
    ids, previous_rows = facts['ids'], facts['previous']
    next_mid, next_pid = facts['next_measurement'], facts['next_prediction']
    measurements, samples, mids, originals = [], [], {}, dict(facts['originals'])
    for key, track in tracks.items():
        if key not in ids or not track or not track['evidence']:
            continue
        evidence, rid = track['evidence'], ids[key]
        for vibe, fit in evidence['fits'].items():
            row = facts['measurements'].get((rid, vibe))
            if row:
                mid = row[0]
                if tuple(row[1:]) != (fit['score'], fit['support'], evidence['source']):
                    raise ValueError('Original measurement identity changed its score')
            else:
                mid, next_mid = next_mid, next_mid + 1
                measurements.append((mid, rid, vibe, fit['score'], fit['support'], evidence['source'],
                                     track['version'], evidence['input_digest'], now))
            originals[mid] = vibe, rid
            mids[key, vibe] = mid
            samples.extend((mid, sid) for sid in evidence.get('sample_ids', []))
    plan = PreparedVibes(measurements, samples, [], [], [], set(touched))
    for key, track in tracks.items():
        rid = ids.get(key)
        if rid is None:
            continue
        previous = previous_rows.get(rid, {})
        plan.deleted.extend((rid, vibe) for vibe in previous.keys() - resolved.get(key, {}).keys())
        if track is None:
            continue
        if track['genre'] and genre_axes.get(track['genre'], (None, None))[1] != track['genre']:
            raise ValueError('Prediction conditioning requires a canonical specific genre key')
        for vibe, fit in resolved.get(key, {}).items():
            if fit.get('retain'):
                continue
            source, mid, pid = fit['source'], mids.get((key, vibe)), None
            if source in ('artist_genre', 'artist', 'genre'):
                model, old = fit.get('generation'), previous.get(vibe)
                if model:
                    artists = json.loads(track['artist'] or '[]')
                    if any(artist not in facts['artists'] for artist in artists):
                        raise ValueError('Unknown conditioning artist')
                    conditioning = fit.get('conditioning')
                    receipt = vibe_model_files.receipt(model, artists, fit['mean_variance'], fit['new_track_variance'],
                                                       conditioning=conditioning)
                    if conditioning is not None and conditioning['genre'] != track['genre']:
                        raise ValueError('Joint target genre differs from publication conditioning')
                    stamp = track['version'] + '/' + fit['model_version']
                    if (old and old['prediction_id'] is not None and old['source'] == source
                            and old['score'] == fit['score'] and old['support'] == fit['support']
                            and old['artist_key'] == track['artist'] and old['subgenre_key'] == track['genre']
                            and old['inputs_version'] == stamp
                            and _same_receipt(facts['provenance'], old['prediction_id'], receipt)):
                        pid = old['prediction_id']
                    else:
                        pid, next_pid = next_pid, next_pid + 1
                        plan.predictions.append((pid, rid, vibe, fit['score'], fit['support'],
                            track['genre'] if source != 'artist' else None, source, stamp, now))
                        plan.provenance.append((pid, receipt, artists))
                        plan.generations.append((pid, bytes.fromhex(model)))
                    values = (rid, vibe, fit['score'], fit['support'], source, None, pid, stamp,
                              track['artist'], track['genre'], fit.get('recordings'), None)
                    if vibe not in previous or tuple(previous[vibe].values()) != values:
                        plan.publications.append(values)
                    continue
                inputs = {mids.get((identity, vibe), original) for identity, original in fit['inputs'].items()}
                if not inputs or None in inputs:
                    raise ValueError('A prediction requires original measurement inputs')
                if (old and old['prediction_id'] is not None and old['source'] == source
                        and old['score'] == fit['score'] and old['support'] == fit['support']
                        and old['inputs_version'] == track['version'] and old['artist_key'] == track['artist']
                        and old['subgenre_key'] == track['genre']
                        and _same_inputs(facts['provenance'], old['prediction_id'], inputs)):
                    pid = old['prediction_id']
                if pid is None:
                    subjects = set()
                    for original in inputs:
                        subject = originals.get(original)
                        if not subject or subject[0] != vibe or subject[1] == rid or subject[1] in subjects:
                            raise ValueError('Prediction inputs require distinct original recordings of the same vibe')
                        subjects.add(subject[1])
                    artists = json.loads(track['artist'] or '[]') if source != 'genre' else []
                    if source != 'genre' and not artists:
                        raise ValueError('Complete calculation requires original artist conditioning')
                    if any(artist not in facts['artists'] for artist in artists):
                        raise ValueError('Unknown conditioning artist')
                    pid, next_pid = next_pid, next_pid + 1
                    plan.predictions.append((pid, rid, vibe, fit['score'], fit['support'],
                        track['genre'] if source != 'artist' else None, source, track['version'], now))
                    plan.provenance.append((pid, inputs, artists))
                mid = None
            values = (rid, vibe, fit['score'], fit['support'], source, mid, pid, track['version'],
                      track['artist'], track['genre'], fit.get('recordings'), fit.get('variance'))
            if vibe not in previous or tuple(previous[vibe].values()) != values:
                plan.publications.append(values)
    return plan


def current(rows, sample_ids):
    if not rows:
        return None
    own = [row for row in rows if row['measurement_id'] is not None]
    evidence = None
    if own:
        evidence = {'source': own[0]['source'], 'input_digest': own[0]['input_digest'],
                    'sample_ids': sorted(set(sample_ids)),
                    'fits': {row['vibe']: {'score': row['score'], 'support': row['support']} for row in own}}
    return {'artist': rows[0]['artist_key'], 'genre': rows[0]['subgenre_key'], 'evidence': evidence,
            'version': rows[0]['inputs_version']}


def add_cohort(stats, inputs, rows):
    for identity, mid, vibe, score, support in rows:
        inputs.setdefault(vibe, {})[identity] = mid
        values = stats.setdefault(vibe, [0, 0., 0., 0.])
        weighted = support * score
        values[0] += 1
        values[1] += support
        values[2] += weighted
        values[3] += weighted * score
