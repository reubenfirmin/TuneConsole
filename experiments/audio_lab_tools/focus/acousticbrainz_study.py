"""Evaluate optional AcousticBrainz evidence for the native Focus specialist."""

import argparse
import bz2
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from threadpoolctl import threadpool_limits

from yt_playlist.core.catalog_query import PLAYABLE_LIBRARY_SQL
from yt_playlist.integrations.acousticbrainz import USER_AGENT
from yt_playlist.providers.acousticbrainz import _prob

from ..studies import individual_protocol as protocol
from ..support.external_experiment import LAB, digest, save, stamp
from ..support.quality_round import seal
from .focus_experiment import arrays, check_receipt, group_guard, head_predictions, inside, load, runtime


SOURCE = LAB / 'local/focus-615-20261030/round-1'
BASELINE_PLAN = SOURCE / 'plan.json'
DEFAULT = LAB / 'local/focus-acousticbrainz-1275/round-2'
CAL500 = LAB / 'local/datasets/cal500/audio/CAL500_32kps'
METADATA = SOURCE / 'inputs/cal500-raw-activity.json'
DATA = SOURCE / 'inputs/cal500-raw-activity.npz'
ROOT = LAB.parent.parent
PACKAGED_FOCUS = ROOT / 'src/yt_playlist/rec/data/focus-vibes.json'
HIGH_LEVEL = 'https://acousticbrainz.org/api/v1/high-level'
MSD_ARCHIVE = ('https://data.metabrainz.org/pub/musicbrainz/acousticbrainz/acousticbrainz-labs/'
               'download/msdtombid/msd-mbid-2016-01-results.json.bz2')
SIGNALS = {'instrumental': ('voice_instrumental', 'instrumental'),
           'relaxed': ('mood_relaxed', 'relaxed')}
FEATURES = {'offset_only': [], 'instrumental': [0], 'relaxed': [1], 'both': [0, 1]}
FOCUS_VERSION = '695f07272d12d218cefa15aca7ec3ee5105592d7ca428248c99c2a5c6f1ca0a8'
RIDGE_PENALTY = 10
# Several distinct, unambiguous original releases are 60 seconds longer than
# their retained CAL500 clips (e.g. SOS, Elected and For You and I). Check both
# lengths and require a unique candidate across them.
DURATION_OFFSETS = (0, 60)
DURATION_TOLERANCE = 3
LIBRARY = f"""
SELECT t.recording_id, t.mb_year, t.mb_recording_id,
       t.instrumental, t.mood_relaxed,
       EXISTS(SELECT 1 FROM recording_identifiers i
              WHERE i.recording_id = t.recording_id AND i.value = t.mb_recording_id
                AND i.scheme = 'musicbrainz' AND i.entity_type = 'recording'
                AND i.source = 'acousticbrainz') AS confirmed_provider_mbid,
       CASE WHEN v.source = 'audio_measurement' AND instr(u.value, ?) > 0
                 THEN v.score END AS specialist
FROM ({PLAYABLE_LIBRARY_SQL}) t
LEFT JOIN recording_vibes v ON v.recording_id = t.recording_id AND v.vibe = 'concentrate'
LEFT JOIN measurements m ON m.id = v.measurement_id
LEFT JOIN vibe_versions u ON u.id = m.model_version_id
ORDER BY t.recording_id
"""


def normalized(text):
    value = unicodedata.normalize('NFKD', text).casefold()
    return ''.join(character for character in value if character.isalnum())


def clip_duration(item):
    identifier = item['id']
    clip = CAL500 / (identifier.removeprefix('cal500:') + '.mp3')
    if digest(clip) != item['audio_sha256']:
        raise ValueError(f'Frozen CAL500 audio changed: {identifier}')
    output = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'csv=p=0', str(clip)], capture_output=True, text=True, check=True)
    return float(output.stdout.strip())


def provider_probabilities(returned, batch):
    if not isinstance(returned, dict) or returned.keys() - {'mbid_mapping'} - set(batch):
        raise ValueError('Invalid AcousticBrainz batch response')
    probabilities = {}
    for mbid in batch:
        submission = returned.get(mbid, {}).get('0', {})
        values = submission.get('highlevel', {})
        probabilities[mbid] = {name: _prob(values, *field) for name, field in SIGNALS.items()}
    return probabilities


def download_crosswalk(directory):
    archive = directory / 'msd-mbid-2016-01-results.json.bz2'
    if archive.exists():
        return archive
    directory.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix('.partial')
    try:
        with urlopen(Request(MSD_ARCHIVE, headers={'User-Agent': USER_AGENT}), timeout=60) as response:
            with temporary.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    if output.tell() > 220 * 1024**2:
                        raise ValueError('Crosswalk exceeds its published size')
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def archive_rows(source):
    """Read the publisher's JSON array in chunks without inflating it in memory."""
    if source.read(1) != '[':
        raise ValueError('Expected the MSD crosswalk JSON array')
    decoder = json.JSONDecoder()
    buffer, offset = '', 0
    while True:
        while offset < len(buffer) and buffer[offset] in ' ,\t\r\n':
            offset += 1
        if offset < len(buffer) and buffer[offset] == ']':
            return
        try:
            row, end = decoder.raw_decode(buffer, offset)
        except json.JSONDecodeError:
            chunk = source.read(1024 * 1024)
            if not chunk:
                raise
            buffer, offset = buffer[offset:] + chunk, 0
            continue
        yield row
        offset = end


def duration_matches(recording, clip):
    return any(abs(recording - clip - shift) <= DURATION_TOLERANCE for shift in DURATION_OFFSETS)


def resolve_recording(plausible):
    if len(plausible) == 1:
        return next(iter(plausible)), 'unique_title_artist_duration'
    same_release = []
    for mbid, evidence in plausible.items():
        for row in evidence:
            album = normalized(row['msd_release'])
            if album and album in {normalized(release) for release in row['musicbrainz_releases']}:
                same_release.append(mbid)
                break
    if len(same_release) == 1:
        return same_release[0], 'unique_release_agreement'
    return None, 'ambiguous' if plausible else 'no_candidate'


def crosswalk_matches(directory):
    archive = download_crosswalk(directory)
    durations = load(directory / 'durations.json')
    identities = {}
    rows = {}
    for item in load(METADATA)['rows']:
        identifier = item['id']
        key = (normalized(item['title']), normalized(item['artist']))
        identities.setdefault(key, []).append(identifier)
        rows[identifier] = {'seconds': durations[identifier], 'candidates': {}}
    with bz2.open(archive, 'rt', encoding='utf-8') as source:
        for entry in archive_rows(source):
            query = entry['query']
            key = (normalized(query['title']), normalized(query['artist_name']))
            if key not in identities or not query.get('duration'):
                continue
            length = float(query['duration'])
            for identifier in identities[key]:
                evidence = rows[identifier]
                if not duration_matches(length, evidence['seconds']):
                    continue
                for match in entry['match']:
                    if (normalized(match['title']) != key[0] or len(match['artists']) != 1
                            or normalized(match['artists'][0]['name']) != key[1]
                            or not match.get('length') or abs(match['length'] / 1000 - length) > DURATION_TOLERANCE
                            or not duration_matches(match['length'] / 1000, evidence['seconds'])):
                        continue
                    match_evidence = evidence['candidates'].setdefault(match['id'], [])
                    match_evidence.append({'msd_id': query['track_id'], 'msd_seconds': length,
                                           'mb_seconds': match['length'] / 1000,
                                           'msd_release': query['release'],
                                           'musicbrainz_releases': [release['title'] for release in match['releases']]})
    for evidence in rows.values():
        evidence['mbid'], evidence['resolution'] = resolve_recording(evidence['candidates'])
    output = {'input_sha256': digest(METADATA), 'archive_sha256': digest(archive),
              'archive_url': MSD_ARCHIVE, 'rows': rows}
    save(directory / 'crosswalk.json', output)
    print(json.dumps(Counter(row['resolution'] for row in rows.values())))


def high_level(directory):
    path = directory / 'crosswalk-acousticbrainz.json'
    matches = load(directory / 'crosswalk.json')['rows']
    cache = load(path) if path.exists() else {'started': stamp(), 'rows': {}}
    pending = sorted({row['mbid'] for row in matches.values() if row['mbid']} - cache['rows'].keys())
    for start in range(0, len(pending), 25):
        batch = pending[start:start + 25]
        url = HIGH_LEVEL + '?' + urlencode({'recording_ids': ';'.join(batch)})
        request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
        with urlopen(request, timeout=30) as response:
            returned = json.load(response)
            remaining = response.headers.get('X-RateLimit-Remaining')
            reset_in = response.headers.get('X-RateLimit-Reset-In')
        cache['rows'].update(provider_probabilities(returned, batch))
        save(path, cache)
        if remaining == '0' and reset_in and reset_in.isdigit():
            time.sleep(int(reset_in) + 1)
    print(f"Queried high-level scores for {len(cache['rows'])} recording IDs")


def era(year):
    if not year or not str(year).isdigit():
        return 'unknown'
    year = int(year)
    if year < 1980:
        return 'pre-1980'
    if year <= 2019:
        return f'{year // 10 * 10}s'
    if year < 2022:
        return '2020–2021'
    return '2022' if year == 2022 else '2023+'


def library_snapshot(database, output):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(LIBRARY, (FOCUS_VERSION,)).fetchall()
    counts = {}
    scores = []
    for row in rows:
        period = era(row['mb_year'])
        match = bool(row['mb_recording_id'])
        instrumental, relaxed = row['instrumental'], row['mood_relaxed']
        # These two legacy scalar fields are AcousticBrainz projections. A
        # provider success alone can indicate that only low-level data exists.
        available = match and instrumental is not None and relaxed is not None
        confirmed = available and row['confirmed_provider_mbid']
        tally = counts.setdefault(period, Counter())
        tally.update({'recordings': 1, 'recording_mbid': int(match), 'cached_both': int(available),
                      'confirmed_cached_both': int(confirmed),
                      'specialist': int(row['specialist'] is not None),
                      'specialist_cached_both': int(row['specialist'] is not None and available),
                      'specialist_confirmed_both': int(row['specialist'] is not None and confirmed)})
        if row['specialist'] is not None:
            scores.append({'era': period, 'specialist': row['specialist'],
                           'signals': [instrumental, relaxed] if confirmed else [None, None]})
    baseline_path = Path(database).with_name('.baseline.json')
    baseline = load(baseline_path) if baseline_path.exists() else {}
    save(output, {'focus_version': FOCUS_VERSION,
                  'baseline': {field: baseline.get(field) for field in ('id', 'schema')},
                  'coverage': counts, 'scores': scores})
    print(json.dumps(counts))


def fit_adjustment(values, residual):
    center = values.mean(axis=0)
    x = values - center
    # Center the features so the unpenalized intercept is a direct estimate
    # of the matched training cohort's calibration offset.
    weights = np.linalg.solve(x.T @ x + RIDGE_PENALTY * np.eye(x.shape[1]), x.T @ residual)
    return {'center': center.tolist(), 'weights': weights.tolist(), 'offset': float(residual.mean())}


def adjust(base, values, model, available):
    scored = base.copy()
    offset = (values[available] - model['center']) @ model['weights'] + model['offset']
    scored[available] = np.clip(base[available] + offset, 0, 1)
    return scored


def freeze(directory, library):
    target = directory / 'plan.json'
    if target.exists():
        raise ValueError('This run is already frozen')
    provider_matches = directory / 'crosswalk.json'
    provider_scores = directory / 'crosswalk-acousticbrainz.json'
    matches, scores = load(provider_matches), load(provider_scores)
    if (matches['input_sha256'] != digest(METADATA)
            or matches['rows'].keys() != {row['id'] for row in load(METADATA)['rows']}
            or scores['rows'].keys() != {row['mbid'] for row in matches['rows'].values() if row['mbid']}):
        raise ValueError('Recording lookup and provider collection must both be complete')
    archive = directory / 'msd-mbid-2016-01-results.json.bz2'
    if digest(archive) != matches['archive_sha256']:
        raise ValueError('Publisher crosswalk differs from the resolved identities')
    snapshot = load(library)
    if snapshot['focus_version'] != FOCUS_VERSION:
        raise ValueError('Library export uses a different Focus specialist')
    if (digest(PACKAGED_FOCUS) != FOCUS_VERSION
            or load(PACKAGED_FOCUS)['heads'] != load(SOURCE / 'exports/direct.json')['heads']):
        raise ValueError('Packaged Focus specialist differs from the archived direct heads')
    library = directory / 'library.json'
    save(library, snapshot)
    prior_inputs = load(BASELINE_PLAN)['inputs']
    if any(digest(path) != prior_inputs[str(path.relative_to(SOURCE))] for path in (METADATA, DATA)):
        raise ValueError('Prior Focus input differs from its frozen plan')
    files = [METADATA, DATA, BASELINE_PLAN, SOURCE / 'exports/direct.json', provider_matches,
             provider_scores, library, directory / 'durations.json', archive]
    for fold in range(5):
        receipt = f'outer-{fold}.receipt.json'
        evidence = check_receipt(SOURCE, receipt)
        files.extend([SOURCE / receipt, *[SOURCE / name for name in evidence['outputs']]])
    dependencies = {Path(module.__file__).resolve() for name, module in list(sys.modules.items())
                    if name.startswith('experiments.audio_lab_tools.') and getattr(module, '__file__', None)}
    dependencies.update((Path(__file__), LAB.parent / 'run.py', PACKAGED_FOCUS))
    code = {}
    for path in sorted(dependencies):
        name = str(path.relative_to(ROOT))
        target_source = directory / 'source' / name
        target_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target_source)
        code[name] = digest(target_source)
    save(target, {'created_at': stamp(), 'runtime': runtime(), 'code': code,
                  'inputs': {str(path.relative_to(ROOT)): digest(path) for path in files},
                  'library': str(library.relative_to(ROOT)), 'features': FEATURES,
                  'ridge_penalty': RIDGE_PENALTY, 'matching_tolerance_seconds': DURATION_TOLERANCE,
                  'matching_known_duration_offsets_seconds': DURATION_OFFSETS,
                  'bootstrap_draws': 1000, 'cutoff': 'top 20% capped at 10 within each artist-held-out fold'})


def comparisons(y, outer, groups, predictions, mask):
    scores = {name: values[mask] for name, values in predictions.items()}
    pairs = [(name, 'specialist') for name in FEATURES]
    pairs.extend((name, 'offset_only') for name in FEATURES if name != 'offset_only')
    return {'rows': int(mask.sum()), 'groups': len(set(groups[mask])),
            'fold_sizes': [int(np.sum(mask & (outer == fold))) for fold in range(5)],
            'metrics': {name: protocol.metrics(y[mask], values, outer[mask]) for name, values in scores.items()},
            'paired': protocol.paired(y[mask], scores, outer[mask], groups[mask], pairs)}


def library_replay(library, models):
    rows = library['scores']
    base = np.array([row['specialist'] for row in rows])
    values = np.array([row['signals'] for row in rows], dtype=float)
    baseline = np.argsort(-base, kind='stable')[:max(1, int(np.ceil(len(base) / 10)))]
    available = np.isfinite(values).all(axis=1)
    result = {'coverage': library['coverage'], 'specialist_rows': len(rows),
              'unmatched_rows': int((~available).sum()), 'top_decile_rows': len(baseline),
              'specialist_top_eras': Counter(rows[i]['era'] for i in baseline), 'replay': {}}
    for name, columns in FEATURES.items():
        updated = adjust(base, values[:, columns], models[name], available)
        top = np.argsort(-updated, kind='stable')[:len(baseline)]
        result['replay'][name] = {'top_eras': Counter(rows[i]['era'] for i in top),
                                  'retained_top': len(set(top) & set(baseline)),
                                  'score_changes': int(np.count_nonzero(updated != base))}
    return result


def evaluate(directory):
    plan_path = directory / 'plan.json'
    plan = load(plan_path)
    if plan['runtime'] != runtime():
        raise ValueError('The experiment runtime changed after freezing the plan')
    for relative, expected in (plan['inputs'] | plan['code']).items():
        if digest(ROOT / relative) != expected:
            raise ValueError(f'Frozen input changed: {relative}')
    for relative, expected in plan['code'].items():
        if digest(directory / 'source' / relative) != expected:
            raise ValueError(f'Frozen source snapshot changed: {relative}')
    data = arrays(DATA)
    metadata = load(METADATA)['rows']
    matches = load(directory / 'crosswalk.json')['rows']
    signals = load(directory / 'crosswalk-acousticbrainz.json')['rows']
    mbids = [matches[item]['mbid'] for item in data['ids']]
    values = np.array([[signals.get(mbid, {}).get(field) for field in SIGNALS] for mbid in mbids], dtype=float)
    matched = np.isfinite(values).all(axis=1)
    outer, groups, y = data['outer'], data['groups'], data['y'].mean(axis=1)
    if list(data['ids']) != [row['id'] for row in metadata]:
        raise ValueError('Archived features and recording identities differ')
    group_guard(groups, outer)
    group_guard([normalized(row['artist']) for row in metadata], outer)
    if set(outer[matched]) != set(outer):
        raise ValueError('Every held-out fold needs at least one matched provider recording')
    for mbid in {value for value in mbids if value is not None}:
        if len({fold for item, fold in zip(mbids, outer, strict=True) if item == mbid}) != 1:
            raise ValueError(f'MusicBrainz recording crosses protected folds: {mbid}')
    predictions = {name: np.empty(len(y)) for name in ('specialist', *FEATURES)}
    coefficients = {}
    with threadpool_limits(limits=1):
        for fold in range(5):
            valid = outer == fold
            train = ~valid & matched
            archive = load(SOURCE / f'models/outer-{fold}.json')
            native = head_predictions(archive['direct'], data['x'], independent=True)
            retained = arrays(SOURCE / f'outer-{fold}.npz')
            if (not np.array_equal(retained['ids'], data['ids'][valid])
                    or not np.allclose(retained['historical__direct'], native[valid], atol=1e-12, rtol=0)):
                raise ValueError('Fold-specific Focus heads differ from the archived held-out scores')
            base = native.clip(0, 1).mean(axis=1)
            predictions['specialist'][valid] = base[valid]
            coefficients[str(fold)] = {}
            for name, columns in FEATURES.items():
                x = values[:, columns]
                model = fit_adjustment(x[train], y[train] - base[train])
                coefficients[str(fold)][name] = model
                # Use complete cases for every comparison so the signals share
                # the same evaluated recordings and the same fallback mask.
                predictions[name][valid] = adjust(base[valid], x[valid], model, matched[valid])

        deployed = load(SOURCE / 'exports/direct.json')['heads']
        base = head_predictions(deployed, data['x'], independent=True).clip(0, 1).mean(axis=1)
        models = {name: fit_adjustment(values[matched][:, columns], (y - base)[matched])
                  for name, columns in FEATURES.items()}

    resolution = Counter(row['resolution'] for row in matches.values())
    output = {'plan_sha256': digest(plan_path), 'created_at': stamp(), 'resolution': resolution,
              'provider_matches': int(matched.sum()), 'unavailable': int((~matched).sum()),
              'matched': comparisons(y, outer, groups, predictions, matched),
              'mixed': comparisons(y, outer, groups, predictions, np.ones(len(y), dtype=bool)),
              'coefficients': coefficients,
              'library': library_replay(load(ROOT / plan['library']), models)}
    evaluation = directory / 'evaluation.json'
    predictions_path = directory / 'predictions.npz'
    np.savez_compressed(predictions_path, ids=data['ids'], groups=groups, outer=outer,
                        available=matched, target=y, **predictions)
    save(evaluation, output)
    seal(directory, 'evaluation', [evaluation, predictions_path])
    print(json.dumps({'resolution': resolution, 'provider_matches': output['provider_matches'],
                     'matched': output['matched']['paired'], 'mixed': output['mixed']['paired'],
                     'library': output['library']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['crosswalk', 'high-level', 'durations', 'library', 'freeze', 'evaluate'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--library', type=Path)
    parser.add_argument('--database', type=Path)
    args = parser.parse_args()
    directory = inside(args.directory)
    if args.action == 'crosswalk':
        crosswalk_matches(directory)
    elif args.action == 'high-level':
        high_level(directory)
    elif args.action == 'durations':
        save(directory / 'durations.json', {row['id']: clip_duration(row) for row in load(METADATA)['rows']})
    elif args.action == 'library':
        library_snapshot(args.database, args.library)
    elif args.action == 'freeze':
        freeze(directory, args.library)
    else:
        evaluate(directory)


if __name__ == '__main__':
    main()
