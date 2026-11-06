"""Pinned #587 evidence ablations on an isolated, complete prepared account."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import resource
import shutil
import socket
import sqlite3
import time

from pytest import MonkeyPatch
from threadpoolctl import threadpool_limits

from .external_experiment import ROOT, digest, save, stamp
from . import replay_runtime

WORK = ROOT / 'experiments/audio_lab/local/output-evaluation-587-20261030'
ARMS = ('metadata', 'signal', 'combined')
NOW = 1793386800.
SEED = 136


def inside(path):
    path = Path(path).resolve()
    if not path.is_relative_to(WORK.resolve()):
        raise ValueError('All evaluation writes must remain inside ' + str(WORK))
    return path


def modules():
    from yt_playlist.rec import (audio_vibe_model, audio_vibe_cruise, audio_vibe_release,
        audio_vibe_relax, audio_vibe_uplift, audio_vibe_energize, audio_vibe_focus)
    return (audio_vibe_model, audio_vibe_cruise, audio_vibe_release,
            audio_vibe_relax, audio_vibe_uplift, audio_vibe_energize, audio_vibe_focus)


def freeze(directory, database):
    from yt_playlist.core import sample_files
    directory = inside(directory)
    directory.mkdir(parents=True, exist_ok=False)
    if not replay_runtime.runtime_identity()['runtime_snapshot']:
        raise ValueError('Freeze a complete runtime first')
    plan = {'created_at': stamp(), 'ticket': 587, 'arms': ARMS, 'seed': SEED, 'clock': NOW,
        **replay_runtime.runtime_identity(), 'runtime_files': replay_runtime.runtime_hashes(),
        'protocol_sha256': digest(ROOT / 'experiments/audio_lab/OUTPUT_EVALUATION_PLAN.md'),
        'script_sha256': digest(__file__), 'database': str(database),
        'helpers': {name: digest(Path(__file__).with_name(name)) for name in
                    ('external_propagation.py', 'external_experiment.py', 'replay_runtime.py')},
        'models': {m.__name__: {'path': str(m.DEFAULT_PATH), 'sha256': digest(m.DEFAULT_PATH)} for m in modules()},
        'budgets': {'publication': 180, 'consumer': 30, 'arm': 900},
        'access': 'Pinned read-only native transaction; all restores/writes in this experiment directory.'}
    save(directory / 'plan.json', plan)
    shutil.copyfile(__file__, directory / 'adapter.py')
    started = time.monotonic()
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute('PRAGMA query_only=ON')
        reader.execute('BEGIN')
        plan['source_schema'] = reader.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        # Immutable file bodies already have exact SQLite owners. Copy each
        # referenced physical file once; avoid reserializing/fsyncing 100k old
        # recommendation chunks through the disaster-recovery publisher.
        columns, names = [], set()
        for table, in reader.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for field in reader.execute('PRAGMA table_info("' + table + '")'):
                column = field[1]
                if 'filename' in column:
                    columns.append([table, column])
                    names.update(row[0] for row in reader.execute(
                        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL') if row[0])
        copied, absent = {}, []
        print(json.dumps({'stage': 'copying_referenced_files', 'files': len(names)}), flush=True)
        for index, name in enumerate(sorted(names)):
            source = Path(name)
            parent = hashlib.sha256(str(source.parent).encode()).hexdigest()[:20]
            target = directory / 'files' / parent / source.name
            if not source.is_absolute():
                raise ValueError('Unresolved relative account filename: ' + name)
            if not source.is_file():
                absent.append(name)
                copied[name] = {'path': str(target), 'sha256': None}
                continue
            target.parent.mkdir(exist_ok=True, parents=True)
            expected = digest(source)
            shutil.copyfile(source, target)
            if digest(target) != expected:
                raise ValueError('Referenced file changed during copy: ' + name)
            copied[name] = {'path': str(target), 'sha256': expected}
            if index and index % 20000 == 0:
                print(json.dumps({'copied_files': index, 'seconds': time.monotonic() - started}), flush=True)
            if time.monotonic() - started > 900:
                raise TimeoutError('Read-only account export exceeded its separate 900 second copy budget')
        manifests = {'columns': columns, 'files': copied, 'missing': absent}
        sample_rows, missing = [], []
        for row in reader.execute('SELECT * FROM sample_attributes ORDER BY id'):
            try:
                target = Path(copied[row['filename']]['path'])
                sample = sample_files.read(target, expected={k: row[k]
                    for k in ('id', 'video_id', 'run_id', 'point')})
                checksum = copied[row['filename']]['sha256']
                sample_rows.append({'id': row['id'], 'filename': str(target), 'sha256': checksum})
            except (OSError, ValueError) as error:
                missing.append({'id': row['id'], 'error': str(error)})
        with closing(sqlite3.connect(directory / 'source.db')) as target:
            backup_started = time.monotonic()
            def bound(_status, _remaining, _total):
                if time.monotonic() - backup_started > 300:
                    raise TimeoutError('SQLite backup exceeded its separate 300 second budget')
            reader.backup(target, pages=1024, progress=bound)
    save(directory / 'files.json', manifests)
    save(directory / 'samples.json', {'rows': sample_rows, 'missing': missing})
    plan.update(freeze_seconds=time.monotonic() - started, database_sha256=digest(directory / 'source.db'),
        manifests_sha256=digest(directory / 'files.json'), samples_sha256=digest(directory / 'samples.json'),
        copied_samples=len(sample_rows), missing_samples=len(missing))
    save(directory / 'plan.json', plan)
    print(json.dumps({k: v for k, v in plan.items() if k not in ('runtime_files', 'models')}), flush=True)


def verify(directory, *, check_script=True):
    plan = json.loads((directory / 'plan.json').read_text())
    if (check_script and plan['script_sha256'] != digest(__file__)
            or plan['runtime_files'] != replay_runtime.runtime_hashes()
            or plan['database_sha256'] != digest(directory / 'source.db')
            or plan['samples_sha256'] != digest(directory / 'samples.json')
            or plan['manifests_sha256'] != digest(directory / 'files.json')):
        raise ValueError('Pinned evaluation inputs changed')
    if plan['protocol_sha256'] != digest(ROOT / 'experiments/audio_lab/OUTPUT_EVALUATION_PLAN.md'):
        raise ValueError('Pinned protocol changed')
    if plan.get('continuation_sha256') and plan['continuation_sha256'] != digest(
            ROOT / 'experiments/audio_lab/OUTPUT_EVALUATION_CONTINUATION_PLAN.md'):
        raise ValueError('Pinned continuation protocol changed')
    if plan.get('consumer_continuation_sha256') and plan['consumer_continuation_sha256'] != digest(
            ROOT / 'experiments/audio_lab/OUTPUT_EVALUATION_CONSUMER_PLAN.md'):
        raise ValueError('Pinned consumer continuation protocol changed')
    for name, expected in plan['helpers'].items():
        if digest(Path(__file__).with_name(name)) != expected:
            raise ValueError('Pinned evaluation helper changed: ' + name)
    for model in plan['models'].values():
        if digest(model['path']) != model['sha256']:
            raise ValueError('Pinned model changed')
    for sample in json.loads((directory / 'samples.json').read_text())['rows']:
        if digest(sample['filename']) != sample['sha256']:
            raise ValueError('Pinned sample changed')
    for value in json.loads((directory / 'files.json').read_text())['files'].values():
        if value['sha256'] and digest(value['path']) != value['sha256']:
            raise ValueError('Pinned account file changed: ' + value['path'])
    return plan


def clone(directory, destination):
    shutil.copyfile(directory / 'source.db', destination)
    with closing(sqlite3.connect(destination)) as conn:
        files = json.loads((directory / 'files.json').read_text())
        conn.execute('CREATE TEMP TABLE copied_paths(old TEXT PRIMARY KEY,new TEXT)')
        conn.executemany('INSERT INTO copied_paths VALUES (?,?)', [(old, v['path']) for old, v in files['files'].items()])
        for table, column in files['columns']:
            conn.execute(f'UPDATE "{table}" SET "{column}"=(SELECT new FROM copied_paths WHERE old="{column}") '
                         f'WHERE "{column}" IN (SELECT old FROM copied_paths)')
        samples = json.loads((directory / 'samples.json').read_text())
        with conn:
            conn.executemany('UPDATE sample_attributes SET filename=? WHERE id=?',
                [(r['filename'], r['id']) for r in samples['rows']] +
                [(str(directory / 'missing-samples' / r['id']), r['id']) for r in samples['missing']])


def revise(source, directory, *, continuation=False, consumer_continuation=False):
    """Pin an adapter repair against the identical retained account, never live data."""
    plan = verify(source, check_script=False)
    directory.mkdir(parents=True, exist_ok=False)
    for name in ('source.db', 'files.json', 'samples.json'):
        shutil.copyfile(source / name, directory / name)
    plan.update(created_at=stamp(), script_sha256=digest(__file__),
                previous_plan={'path': str(source / 'plan.json'), 'sha256': digest(source / 'plan.json')})
    if continuation:
        plan.update(budgets={'publication': 600, 'consumer': 30, 'arm': 1800},
            original_budgets={'publication': 180, 'consumer': 30, 'arm': 900},
            continuation_sha256=digest(ROOT / 'experiments/audio_lab/OUTPUT_EVALUATION_CONTINUATION_PLAN.md'),
            prior_attempts={str(p): digest(p) for p in source.glob('*/report.json')})
    if consumer_continuation:
        plan.update(budgets={'publication': 600, 'consumer': 180, 'arm': 1800},
            consumer_continuation_sha256=digest(ROOT / 'experiments/audio_lab/OUTPUT_EVALUATION_CONSUMER_PLAN.md'),
            prior_attempts={**plan.get('prior_attempts', {}),
                            **{str(p): digest(p) for p in source.glob('*/report.json')}})
    for name in ('radio-seed.json', 'radio-exclusions.json'):
        if (source / name).exists():
            shutil.copyfile(source / name, directory / name)
    shutil.copyfile(__file__, directory / 'adapter.py')
    save(directory / 'plan.json', plan)
    print(json.dumps({'revised_plan': str(directory / 'plan.json')}), flush=True)


def configure(patch, output):
    from yt_playlist.core import features
    patch.setenv('YT_PLAYLIST_HOME', str(output / 'app-home'))
    patch.setattr(features, 'LOCAL_ENV', output / '.env.local')
    for index, module in enumerate(modules()):
        patch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
        if index:
            patch.setenv(module.MODE_KEY, 'active')
    def offline(*_args, **_kwargs):
        raise AssertionError('Offline evaluation attempted a network connection')
    patch.setattr(socket.socket, 'connect', offline)


def inputs(store):
    """Use retained metadata profiles and original own measurements, never labels."""
    from yt_playlist.rec import musical_character, musical_profiles, vibe_preparation
    from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
    from .external_propagation import current_inputs
    conn = store.conn
    records = {row['id']: dict(row) for row in conn.execute('SELECT id,identity_key,title,artist FROM recordings')}
    base = {row['identity_key']: musical_character.compact(json.loads(row['payload']))
            for row in conn.execute('SELECT identity_key,payload FROM rec_musical_profiles')}
    # Musical dimensions are prepared from metadata independently of vibe fits.
    # Remove all old vibe predictions, including their old model dependencies.
    for profile in base.values():
        if profile:
            profile.pop('vibes', None)
    library = {'recording_vibes': [dict(r) for r in conn.execute('SELECT * FROM recording_vibes')],
               'measurements': [dict(r) for r in conn.execute('SELECT m.* FROM measurements m JOIN '
                   '(SELECT DISTINCT measurement_id id FROM recording_vibes WHERE measurement_id IS NOT NULL) v USING(id)')]}
    metadata, provider = current_inputs(library, {'provider_measurement'})
    acoustic, partial, invalid, capture_ids = {}, [], [], {}
    captured = [row[0] for row in conn.execute('SELECT DISTINCT r.identity_key FROM sample_attributes s '
                                              'JOIN recordings r ON r.id=s.recording_id ORDER BY r.identity_key')]
    ids = {r['identity_key']: rid for rid, r in records.items()}
    for key in captured:
        item = store.vibe_predictions.inputs([key])[key]
        rid = ids[key]
        if not item['samples']:
            partial.append(key)
            continue
        try:
            acoustic[rid] = vibe_preparation.measured(item, [], {}, {})
            capture_ids[key] = sorted(s['id'] for s in item['samples'])
            # Every original measurement can contribute once through its actual
            # artist/genre cohort; no score copied from a propagated prediction.
            metadata.setdefault(rid, {'artist': item['artist'], 'genre': None})
        except (ValueError, KeyError) as error:
            invalid.append({'key': key, 'error': str(error)})
    for own in provider.values():
        for channel, vibe in INTENSITY_CHANNELS.items():
            if vibe in own['fits']:
                own['fits'].setdefault(channel, dict(own['fits'][vibe]))
    return {'records': records, 'base': base, 'metadata': metadata, 'provider': provider,
            'acoustic': acoustic, 'partial': partial, 'invalid': invalid, 'capture_ids': capture_ids}


def profiles(data, arm, *, first_excerpts=False):
    from yt_playlist.rec import home_vibe, musical_character, musical_profiles, vibe_prediction
    from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
    from .external_propagation import cohorts
    acoustic = {} if first_excerpts else data['acoustic']
    own = (data['provider'] if arm == 'metadata' else acoustic if arm == 'signal'
           else {**data['provider'], **acoustic})
    statistics = {} if arm == 'signal' else cohorts(data['metadata'], own)
    empty = musical_character.compact(musical_profiles.resolve([]))
    result = {}
    for rid, row in data['records'].items():
        key = row['identity_key']
        profile = deepcopy(empty if arm == 'signal' else data['base'].get(key) or empty)
        evidence, groups = own.get(rid), []
        if arm != 'signal' and rid in data['metadata']:
            for group in vibe_prediction.groups(**data['metadata'][rid]):
                stats = statistics.get(group, {})
                groups.append((group, vibe_prediction.adjust(stats, evidence, -1) if evidence else stats))
        fallback = {v: home_vibe.heuristic_affinity(profile, v) for v in home_vibe.PRESETS}
        fallback.update({channel: fallback[vibe] for channel, vibe in INTENSITY_CHANNELS.items()})
        fits = vibe_prediction.resolve(fallback, evidence, groups)
        fits['intensity_fits'] = {vibe: fits['fits'].pop(channel) for channel, vibe in INTENSITY_CHANNELS.items()}
        profile['vibes'] = fits
        profile.pop('digest', None)
        profile['digest'] = musical_profiles.digest(profile)
        result[key] = profile
    return result


def run(directory, arm, *, resume=None):
    from yt_playlist.core.store import Store
    from yt_playlist.core.activity import cancellable_work
    from yt_playlist.rec import home_vibe, musical_character, mix_tuning, mode_surfaces, radio_tuning, vibe_pools
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from yt_playlist.rec.radio_directions import RadioDirections
    from yt_playlist.rec.radio_genres import GenrePolicy, genre
    from experiments.investigations.bench_musical_character import _vector_digest
    if arm not in ARMS:
        raise ValueError('Choose one declared arm')
    plan = verify(directory)
    output = directory / (arm + '-' + stamp().replace(':', '').replace('+', '_'))
    output.mkdir()
    report = {'created_at': stamp(), 'arm': arm, 'plan_sha256': digest(directory / 'plan.json'),
        'complete': False, 'measurements': {}, 'annotation_reads': 0, 'phases': {}, 'violations': [],
        'limits': ['Prepared-account profile adapter; not startup, UI, deployment or playback acceptance.',
                   'Uses retained metadata profile dimensions; pending metadata preparation is not silently completed.',
                   'Timings include research adapter and controlled offline execution.',
                   'No human whole-output judgments or intensity calibration labels.']}
    save(output / 'report.json', report)
    started = time.monotonic()

    def measure(name, function, seconds=None):
        seconds = plan['budgets']['consumer'] if seconds is None else seconds
        wall, cpu = time.monotonic(), time.process_time()
        try:
            with cancellable_work(lambda: time.monotonic() - wall > seconds or
                                           time.monotonic() - started > plan['budgets']['arm']):
                return function()
        finally:
            report['measurements'][name] = {'wall_seconds': time.monotonic() - wall,
                'cpu_seconds': time.process_time() - cpu, 'limit_seconds': seconds}
            save(output / 'report.json', report)
            print(json.dumps({'stage': name, **report['measurements'][name]}), flush=True)

    with MonkeyPatch.context() as patch:
        configure(patch, output)
        database = output / 'account.db'
        previous = None
        if resume:
            previous = json.loads((resume / 'report.json').read_text())
            old_plan = json.loads((resume.parent / 'plan.json').read_text())
            if (previous['arm'] != arm or previous['plan_sha256'] != digest(resume.parent / 'plan.json') or
                    any(old_plan[key] != plan[key] for key in
                        ('database_sha256', 'models', 'runtime_files', 'clock', 'seed')) or
                    not previous.get('publication_heads')):
                raise ValueError('Consumer continuation does not match the pinned arm/publication')
            measure('clone_published_attempt', lambda: shutil.copyfile(resume / 'account.db', database), seconds=300)
            report['reused_publication'] = {'path': str(resume), 'report_sha256': digest(resume / 'report.json'),
                                           'prior_publication': previous['measurements']['publication']}
        else:
            measure('clone', lambda: clone(directory, database), seconds=300)
        store = Store(database)
        try:
            measure('admission', store.init_schema, seconds=60)
            store.settings.set_setting('rec_selection_paused', '1')
            mix_tuning.reset(store)
            cf_before = _vector_digest(store)
            report['remaining_preparation'] = {name: store.conn.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                for name in ('rec_content_pending', 'rec_musical_pending', 'rec_vibe_pending')}
            data = measure('input_inventory', lambda: inputs(store), seconds=180)
            report['inventory'] = {'recordings': len(data['records']), 'metadata_profiles': len(data['base']),
                'provider_recordings': len(data['provider']), 'complete_audio_recordings': len(data['acoustic']),
                'missing_or_partial_captures': data['partial'], 'invalid_captures': data['invalid'],
                'profile_conflicts': sum(any(d.get('conflict') for d in p.get('dimensions', {}).values())
                                         for p in data['base'].values() if p),
                'metadata_missing_genre': sum(not m['genre'] for m in data['metadata'].values())}
            profile_map = measure('profiles', lambda: profiles(data, arm), seconds=180)
            save(output / 'capture-joins.json', data['capture_ids'])
            save(output / 'profiles.json', profile_map)
            report['profiles_sha256'] = digest(output / 'profiles.json')
            if previous and report['profiles_sha256'] != previous['profiles_sha256']:
                raise ValueError('Consumer continuation changed its musical profiles')
            profile_calls = Counter()
            def load(_store, keys):
                profile_calls['calls'] += 1
                profile_calls['keys'] += len(keys)
                return {key: profile_map.get(key) for key in keys}
            patch.setattr(musical_character, 'load', load)
            # Protect every subsequently opened consumer connection too.
            def authorize(action, table, _column, _db, _source):
                if action == sqlite3.SQLITE_READ and table and table.startswith('audio_annotation'):
                    report['annotation_reads'] += 1
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            store.conn.set_authorizer(authorize)
            original_connect = sqlite3.connect
            def guarded_connect(*args, **kwargs):
                conn = original_connect(*args, **kwargs)
                conn.set_authorizer(authorize)
                return conn
            patch.setattr(sqlite3, 'connect', guarded_connect)
            # A full candidate publication is necessary: old intensity bands
            # cannot be reused after ablation even if track identities match.
            if previous:
                if store.recommendations.heads() != previous['publication_heads']:
                    raise ValueError('Consumer continuation changed publication heads')
            else:
                bundles = measure('publication', lambda: mode_surfaces.prepare_bundles(store, NOW),
                                  seconds=plan['budgets']['publication'])
                if not bundles:
                    raise ValueError('No complete prepared publication within the declared budget')
            report['publication_heads'] = store.recommendations.heads()
            seed_path = directory / 'radio-seed.json'
            if seed_path.exists():
                seed = json.loads(seed_path.read_text())
            else:
                with closing(RadioCandidates(store, NOW, radio_tuning.DEFAULTS, comparison=None)) as candidate:
                    candidate.rng.seed(SEED)
                    seed = next(iter(candidate.seed_offer(1)), None)
                if not seed:
                    raise ValueError('Account supplied no eligible fixed Radio seed')
                seed.pop('musical', None)
                save(seed_path, seed)
            report['seed_sha256'] = digest(seed_path)
            genre_axes = store.genres.feature_axes({m['genre'] for m in data['metadata'].values() if m['genre']})
            genres = [family for family, _count in Counter(genre_axes[m['genre']][0]
                for m in data['metadata'].values() if m['genre']).most_common(2)]
            report['explicit_genres'] = genres

            def episode(dial, *, excluded=(), restricted=False, label='episode'):
                tuning = {**radio_tuning.DEFAULTS, 'energy': dial}
                if restricted:
                    tuning['theme'] = 1
                with closing(RadioCandidates(store, NOW, tuning, comparison=None)) as candidate:
                    candidate.rng.seed(SEED)
                    history = [{**deepcopy(seed), 'musical': profile_map.get(seed['key'])}]
                    try:
                        for _ in range(23):
                            following = candidate.next_track(history, excluded=set(excluded))
                            if following is None:
                                break
                            history.append(dict(following))
                    finally:
                        save(output / (label + '-trace.json'), history)
                    return history

            def outputs(phase):
                mixes, radio = {}, {}
                for vibe in home_vibe.PRESETS:
                    mixes[vibe] = measure(phase + ':mix:' + vibe,
                        lambda vibe=vibe: vibe_pools.assemble(store, NOW, vibe))
                    save(output / (phase + '-mixes.json'), mixes)
                for dial in (0, .5, 1):
                    radio[str(dial)] = measure(phase + ':radio:' + str(dial),
                        lambda dial=dial: episode(dial, label=phase + '-radio-' + str(dial)))
                    save(output / (phase + '-radio.json'), radio)
                report['phases'][phase] = {'mix_lengths': {v: [len(c['tracks']) for c in cards]
                    for v, cards in mixes.items()}, 'radio_lengths': {k: len(v) for k, v in radio.items()}}
                return mixes, radio

            mixes, radio = outputs('complete')
            restrictions = {}
            for vibe in home_vibe.PRESETS:
                for family in genres:
                    for kind in ('include', 'avoid'):
                        name = ':'.join((vibe, kind, family))
                        cards = measure('restriction:' + name, lambda vibe=vibe, family=family, kind=kind:
                            vibe_pools.assemble(store, NOW, vibe, **{kind + '_genres': [family]}))
                        tracks = [t for c in cards for t in c['tracks']]
                        axes = store.genres.feature_axes({t.get('genre') for t in tracks})
                        invalid = [t['key'] for t in tracks if
                            (axes[t.get('genre')][0] == family) != (kind == 'include')]
                        restrictions[name] = {'tracks': tracks, 'violations': invalid}
                        if invalid:
                            report['violations'].append(name)
            # Pick excluded identities once; reuse them for every evidence arm.
            blocked_path = directory / 'radio-exclusions.json'
            if not blocked_path.exists():
                save(blocked_path, [t['video_id'] for t in radio['0.5'][1:4]])
            blocked = json.loads(blocked_path.read_text())
            for dial in (0, .5, 1):
                name = 'radio:' + str(dial)
                tracks = measure('restriction:' + name, lambda dial=dial: episode(dial, excluded=blocked,
                    restricted=True, label='restricted-radio-' + str(dial)))
                policy = GenrePolicy(1, [seed], current=seed, store=store)
                invalid = [t['video_id'] for t in tracks[1:] if not policy.allows(t) or t['video_id'] in blocked]
                restrictions[name] = {'tracks': tracks, 'violations': invalid, 'excluded': blocked}
                if invalid:
                    report['violations'].append(name)
            save(output / 'restrictions.json', restrictions)
            for index in range(3):
                for vibe in home_vibe.PRESETS:
                    measure(f'warm-mix:{vibe}:{index}', lambda vibe=vibe: vibe_pools.assemble(store, NOW, vibe))
                def retune():
                    with closing(RadioCandidates(store, NOW, radio_tuning.DEFAULTS, comparison=None)) as base:
                        with closing(RadioCandidates(store, NOW, {**radio_tuning.DEFAULTS, 'energy': index % 2},
                                now_source=base, comparison=None)) as candidate:
                            candidate.rng.seed(SEED)
                            current = {**seed, 'musical': profile_map.get(seed['key'])}
                            RadioDirections(candidate, current).update(radio['0.5'][:6], [], 5, force=True)
                measure('warm-retune:' + str(index), retune)
            if arm != 'metadata':
                # Production preparation requires a complete playback run. With
                # just its first excerpt there is no new audio measurement;
                # provider/metadata fallback is retained exactly as production.
                profile_map = profiles(data, arm, first_excerpts=True)
                partial_bundles = measure('first-excerpts-publication',
                    lambda: mode_surfaces.prepare_bundles(store, NOW), seconds=plan['budgets']['publication'])
                if not partial_bundles:
                    raise ValueError('First-excerpt publication did not complete')
                outputs('first-excerpts')
            report['collaborative_vectors_preserved'] = cf_before == _vector_digest(store)
            report['profile_adapter_calls'] = dict(profile_calls)
            report['complete'] = report['collaborative_vectors_preserved'] and not report['violations']
        except BaseException as error:
            report['error'] = repr(error)
            raise
        finally:
            store.conn.close()
            report['wall_seconds'] = time.monotonic() - started
            report['process_peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            save(output / 'report.json', report)
    verify(directory)
    save(output / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
        'outputs': {p.name: digest(p) for p in output.glob('*.json') if p.name != 'receipt.json'}})
    print(json.dumps({'output': str(output), 'complete': report['complete'], 'phases': report['phases']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'run', 'revise'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--database', type=Path, default=Path.home() / '.local/share/yt-playlist/state.db')
    parser.add_argument('--arm', choices=ARMS)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--continuation', action='store_true')
    parser.add_argument('--consumer-continuation', action='store_true')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(args.directory, args.database)
        elif args.action == 'revise':
            revise(inside(args.source), inside(args.directory), continuation=args.continuation,
                   consumer_continuation=args.consumer_continuation)
        else:
            run(inside(args.directory), args.arm, resume=inside(args.resume) if args.resume else None)


if __name__ == '__main__':
    main()
