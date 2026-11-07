"""Frozen representation follow-ups reusing the matched #708 development cohort."""
import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .. import source_path
from . import encoder_models as models, individual_protocol as protocol
from ..support import quality_round as evidence
from .encoder_experiment import DEFAULT as ORIGINAL, primary
from ..audio.encoder_assets import ROOT as ASSETS, MODEL, PYTHON
from ..support.external_experiment import LAB, digest, pcm, save, stamp
from ..focus.focus_experiment import arrays, check_receipt, inside, load
from .mapped_vibe_rules import apply
from .memo_confirmation import holdout_pairs
from ..audio.quality_reference_audio import response

DEFAULT = LAB / 'local/mert-layers-809-20261101/round-2'
WORKER = source_path('mert_layers_worker.py')


def freeze(directory, extraction_source=None):
    evidence.verify(ORIGINAL)
    old = load(ORIGINAL / 'plan.json')
    inputs = {name.removeprefix('inputs/'): ORIGINAL / name for name in old['inputs']}
    inputs.update({'baseline-features.npz': ORIGINAL / 'features.npz',
                   'baseline-predictions.npz': ORIGINAL / 'predictions.npz',
                   'baseline-rows.json': ORIGINAL / 'extraction.json',
                   'baseline-trace.json': ORIGINAL / 'fit-trace.json'})
    specification = {'baseline': str(ORIGINAL),
        'baseline_plan_sha256': digest(ORIGINAL / 'plan.json'),
        'comparison': 'middle versus mert final; matched musicnn secondary',
        'baseline_variant': 'mert', 'new_variants': ['middle'],
        'variants': ['musicnn', 'mert', 'middle'], 'cohort': old['protocol'],
        'fixed_hidden_states': [5, 6], 'dimensions': 1536,
        'preprocessing': 'Identical original MERT 24kHz exact PCM and processor; original MusiCNN 16kHz vectors retained.',
        'budgets': {'extract_seconds': 7200, 'fit_evaluate_seconds': 3600,
                    'rss_bytes': 4 * 1024**3, 'feature_bytes': 256 * 1024**2},
        'parity_max_error': 1e-6, 'replay_max_error': 1e-6,
        'acceptance': old['protocol']['acceptance']}
    reuse_references = []
    if extraction_source is not None:
        extraction_source = inside(extraction_source)
        evidence.verify(extraction_source, required_stages=['extraction'])
        previous = load(extraction_source / 'plan.json')
        ignored = {'document_sha256', 'extraction_reuse'}
        if previous['ticket'] != 809 or ({k: v for k, v in previous['protocol'].items() if k not in ignored}
                != {k: v for k, v in specification.items() if k not in ignored}):
            raise ValueError('Extraction reuse requires the identical representation protocol')
        specification['extraction_reuse'] = str(extraction_source)
        reuse_references = [extraction_source / 'plan.json', extraction_source / 'extraction.receipt.json']
    evidence.freeze(directory, 809, specification, inputs,
        references=[ORIGINAL / 'plan.json', ORIGINAL / 'extraction.receipt.json',
                    ORIGINAL / 'predictions.receipt.json', ORIGINAL / 'pcm-audit.json',
                    ORIGINAL / 'pcm-audit.receipt.json', ASSETS / 'assets.json',
                    ASSETS / 'requirements.lock', *reuse_references,
                    *[MODEL / r['name'] for r in load(ASSETS / 'assets.json')['files']]],
        documents=[WORKER, Path(__file__).with_name('test_representation_study.py'),
                   LAB.parent / 'investigations/representation_809.py'])
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json')}), flush=True)


def reuse_extraction(directory):
    """Copy only receipt-verified extraction outputs into a new source freeze."""
    source = inside(load(directory / 'plan.json')['protocol']['extraction_reuse'])
    receipt = check_receipt(source, 'extraction.receipt.json')
    outputs = []
    for name, checksum in receipt['outputs'].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory):
            raise ValueError('Extraction output escaped the round')
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, path)
        if digest(path) != checksum:
            raise ValueError('Copied extraction changed')
        outputs.append(path)
    evidence.seal(directory, 'extraction', outputs)


def check_chunk(record, identity, expected):
    if record['input'] != identity:
        raise ValueError('Cached chunk identity changed')
    from ..datasets.dataset_windows import content_hash
    if content_hash(record['result']) != record['result_sha256']:
        raise ValueError('Cached chunk content changed')
    result = record['result']
    vector = np.asarray(result['embedding'])
    if vector.shape != (1536,) or not np.isfinite(vector).all() or result['hidden_states'] != 13:
        raise ValueError('Invalid middle-layer output')
    final = np.asarray(result['final_embedding'])
    if final.shape != (768,) or not np.isfinite(final).all():
        raise ValueError('Invalid final-layer output')
    error = float(np.max(np.abs(final - expected)))
    if error > 1e-6:
        raise ValueError('Final-layer feature parity failed')
    return vector, error


def extract(directory):
    if (directory / 'extraction.receipt.json').exists():
        check_receipt(directory, 'extraction.receipt.json')
        return
    from ..datasets.dataset_windows import content_hash
    rows = load(directory / 'inputs/baseline-rows.json')['rows']
    original = arrays(directory / 'inputs/baseline-features.npz')
    pcm_audit = load(ORIGINAL / 'pcm-audit.json')['intervals']
    checksums = {(r['id'], r['start']): r['pcm_24000_sha256'] for r in pcm_audit}
    started = time.monotonic()
    env = {**os.environ, 'HF_HOME': str(ASSETS / 'hf-cache'),
           'HF_MODULES_CACHE': str(ASSETS / 'hf-cache/modules'), 'HF_HUB_OFFLINE': '1',
           'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'}
    chunks = np.zeros((*original['mert'].shape[:2], 1536))
    outputs, audit = [], []
    replay_errors, replay_seconds = [], []
    worker_hash = digest(WORKER)
    with TemporaryDirectory(prefix='layers-pcm-', dir=directory) as temporary, (directory / 'worker.log').open('a') as log:
        worker = subprocess.Popen([str(PYTHON), '-B', str(WORKER), str(MODEL)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, env=env)
        try:
            runtime = response(worker, 'READY ')
            checked_audio = set()
            for i, row in enumerate(rows):
                if row['audio_sha256'] not in checked_audio:
                    if digest(row['audio']) != row['audio_sha256']:
                        raise ValueError('Original audio changed')
                    checked_audio.add(row['audio_sha256'])
                for j, start in enumerate(row['chunk_starts']):
                    if time.monotonic() - started > 7200:
                        raise RuntimeError('Frozen extraction deadline exceeded')
                    identity = {'audio_sha256': row['audio_sha256'], 'start': start, 'seconds': 10,
                                'states': [5, 6], 'worker_sha256': worker_hash,
                                'assets_sha256': digest(directory / 'inputs/encoder-assets.json')}
                    path = directory / 'layer-chunks' / (content_hash(identity) + '.json')
                    signal = Path(temporary) / 'interval.f32'
                    pcm(row['audio'], start, 24000, 1, signal)
                    checksum = digest(signal)
                    if signal.stat().st_size != 960000 or checksum != checksums[(row['id'], start)]:
                        raise ValueError('Original interval PCM parity failed')
                    if path.exists():
                        record = load(path)
                    else:
                        worker.stdin.write(json.dumps({'id': row['id'], 'pcm': str(signal)}) + '\n')
                        worker.stdin.flush()
                        result = response(worker, 'RESULT ')
                        if result['id'] != row['id']:
                            raise ValueError('Worker row order changed')
                        result['pcm_sha256'] = checksum
                        record = {'input': identity, 'result': result, 'result_sha256': content_hash(result)}
                        save(path, record)
                    vector, error = check_chunk(record, identity, original['mert'][i, j])
                    if record['result']['pcm_sha256'] != checksum:
                        raise ValueError('Cached PCM identity changed')
                    chunks[i, j] = vector
                    audit.append({'id': row['id'], 'start': start, 'pcm_sha256': checksum,
                                  'final_max_error': error, 'seconds': record['result']['seconds'],
                                  'peak_rss_bytes': record['result']['peak_rss_bytes']})
                    outputs.append(path)
                print(json.dumps({'layers_extracted': i + 1, 'source': row['source'], 'id': row['id']}), flush=True)
            pcm(rows[0]['audio'], rows[0]['chunk_starts'][0], 24000, 1, signal)
            for _ in range(3):
                worker.stdin.write(json.dumps({'id': 'replay', 'pcm': str(signal)}) + '\n')
                worker.stdin.flush()
                repeated = response(worker, 'RESULT ')
                replay_errors.append(float(np.max(np.abs(chunks[0, 0] - repeated['embedding']))))
                replay_seconds.append(repeated['seconds'])
            if max(replay_errors) > 1e-6:
                raise ValueError('Middle-layer replay failed')
        finally:
            worker.stdin.close()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)
    np.savez_compressed(directory / 'features.npz', middle=chunks, musicnn=original['musicnn'],
                        mert=original['mert'], counts=original['counts'])
    cache_bytes = sum(p.stat().st_size for p in (directory / 'layer-chunks').glob('*.json'))
    if cache_bytes + (directory / 'features.npz').stat().st_size > 256 * 1024**2:
        raise RuntimeError('Frozen feature-storage bound exceeded')
    save(directory / 'extraction.json', {'rows': rows, 'audit': audit,
        'resources': {'seconds': time.monotonic() - started, 'runtime': runtime,
            'peak_worker_rss_bytes': max(r['peak_rss_bytes'] for r in audit),
            'new_feature_bytes': (directory / 'features.npz').stat().st_size + cache_bytes,
            'replay_max_error': max(replay_errors), 'replay_seconds': replay_seconds,
            'final_layer_max_error': max(r['final_max_error'] for r in audit),
            'prior_extraction_cost': load(directory / 'inputs/baseline-rows.json')['resources']}})
    evidence.seal(directory, 'extraction', [directory / 'features.npz', directory / 'extraction.json', *outputs])


def fit(directory):
    check_receipt(directory, 'extraction.receipt.json')
    if (directory / 'predictions.receipt.json').exists():
        check_receipt(directory, 'predictions.receipt.json')
        return
    started = time.monotonic()
    plan = load(directory / 'plan.json')['protocol']
    rows = load(directory / 'extraction.json')['rows']
    data = arrays(directory / 'features.npz')
    groups, outer = (np.asarray([r[k] for r in rows]) for k in ('component', 'outer'))
    protocol.group_guard(groups, outer)
    weights = load(directory / 'inputs/selection.json')['weights']
    positions = {s: np.array([i for i, r in enumerate(rows) if r['source'] == s]) for s in models.SOURCES}
    baseline = arrays(directory / 'inputs/baseline-predictions.npz')
    y = {s: baseline['truth__' + s] for s in models.SOURCES}
    result = dict(baseline)
    traces, artifacts, replay_evidence = {}, [], {}
    for variant in plan['new_variants']:
        chunks, counts = data[variant], data['counts']
        native = {s: np.zeros((len(rows), len(models.NATIVE[s]))) for s in models.SOURCES}
        for vibe in models.VIBES:
            result[f'{variant}__oof__{vibe}'] = np.empty(len(rows))

        def context(name, protected, omitted=()):
            if time.monotonic() - started > 3600:
                raise RuntimeError('Frozen fitting deadline exceeded')
            heads, details = {}, {}
            for source in models.SOURCES:
                if source in omitted:
                    continue
                ids = positions[source]
                keep = np.array([g not in protected for g in groups[ids]])
                selected = ids[keep]
                if len(set(groups[selected])) < 3:
                    raise ValueError('Insufficient native groups')
                heads[source], detail = models.choose(chunks[selected], counts[selected], y[source][keep], groups[selected])
                details[source] = {**detail, 'training_ids': [rows[i]['id'] for i in selected]}
            pack = directory / f'models/{variant}-{name}.json'
            save(pack, heads)
            artifacts.append(pack)
            traces[variant + '__' + name] = {'protected_components': sorted(map(int, protected)),
                                            'omitted_sources': list(omitted), 'sources': details}
            print(json.dumps({'representation_fit': variant, 'context': name}), flush=True)
            return heads

        first = None
        for fold in range(5):
            valid = outer == fold
            heads = context('outer-' + str(fold), set(groups[valid]))
            first = first or heads
            values = models.predict(heads, chunks[valid], counts[valid])
            for source, value in values.items():
                native[source][valid] = value
            for vibe in models.VIBES:
                result[f'{variant}__oof__{vibe}'][valid] = models.mixture(vibe, values, weights)
        for source, values in native.items():
            result[f'{variant}__native__{source}'] = values
        for omitted in sorted({models.family(s) for s in models.SOURCES}):
            valid = np.array([r['source'] in omitted for r in rows])
            heads = context('omit-' + omitted[0], set(groups[valid]), omitted)
            values = models.predict(heads, chunks[valid], counts[valid])
            for vibe in models.VIBES:
                value = models.mixture(vibe, values, weights)
                if value is None:
                    continue
                full = np.full(len(rows), np.nan)
                full[valid] = value
                result[f'{variant}__omit-{omitted[0]}__{vibe}'] = full
        reference = models.predict(first, chunks, counts)
        errors, seconds = [], []
        for _ in range(3):
            tick = time.perf_counter()
            repeated = models.predict(first, chunks, counts)
            seconds.append(time.perf_counter() - tick)
            errors.append(max(float(np.max(np.abs(reference[s] - repeated[s]))) for s in reference))
        replay_evidence[variant] = {'seconds': seconds, 'maximum_error': max(errors)}
    np.savez_compressed(directory / 'predictions.npz', **result)
    save(directory / 'fit-trace.json', {'contexts': traces, 'resources': {
        'seconds': time.monotonic() - started, 'head_replay': replay_evidence,
        'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        'head_bytes': sum(p.stat().st_size for p in artifacts)}})
    evidence.seal(directory, 'predictions', [directory / 'predictions.npz', directory / 'fit-trace.json', *artifacts])


def advancement(vibe, tables, comparison):
    cases = {k: t for k, t in tables.items() if t['vibe'] == vibe and t['primary']}
    # Reuse the exact frozen #708 gate, with explicit comparison projection.
    from .encoder_experiment import decision
    projected = {k: {**t, 'paired': {'mert__vs__musicnn': t['paired'][comparison]}} for k, t in cases.items()}
    return {**decision(vibe, projected), 'comparison': comparison}


def evaluate(directory):
    check_receipt(directory, 'predictions.receipt.json')
    started = time.monotonic()
    plan = load(directory / 'plan.json')['protocol']
    variants, baseline = plan['variants'], plan['baseline_variant']
    comparisons = list(dict.fromkeys((v, b) for v in plan['new_variants']
                                    for b in (baseline, 'musicnn') if v != b))
    rows = load(directory / 'extraction.json')['rows']
    prediction = arrays(directory / 'predictions.npz')
    tables, native = {}, {}
    for source in models.SOURCES:
        ids = np.array([i for i, r in enumerate(rows) if r['source'] == source])
        groups, outer = (np.array([rows[i][key] for i in ids]) for key in ('component', 'outer'))
        meta = load(directory / f'inputs/{source}.json')
        yy = arrays(directory / f'inputs/{source}.npz')['y'][[rows[i]['row_index'] for i in ids]]
        for j, label in enumerate(models.NATIVE[source]):
            y = prediction['truth__' + source][:, j]
            scores = {v: prediction[f'{v}__native__{source}'][ids, j] for v in variants}
            native[source + '__' + label] = {'rows': len(y), 'errors': {
                v: {'mse': float(np.mean((p-y)**2)), 'mae': float(np.mean(np.abs(p-y)))} for v, p in scores.items()},
                'paired': protocol.paired(y, scores, outer, groups, comparisons, 1000)}
        for vibe, expression in models.expressions(source).items():
            y = apply(expression, yy, meta['target_names'])
            for mode in ('oof', 'omit-' + models.family(source)[0]):
                if f'{baseline}__{mode}__{vibe}' not in prediction:
                    continue
                scores = {v: prediction[f'{v}__{mode}__{vibe}'][ids] for v in variants}
                scores['current_exposed'] = prediction['current__' + vibe][ids]
                if any(not np.isfinite(p).all() for p in scores.values()):
                    raise ValueError('Missing paired predictions')
                split = outer if mode == 'oof' else np.zeros(len(y), dtype=int)
                paired = (protocol.paired(y, scores, split, groups, comparisons, 1000) if mode == 'oof'
                          else holdout_pairs(y, scores, groups, comparisons, 1000))
                tables[mode + '__' + source + '__' + vibe] = {'vibe': vibe, 'source': source, 'mode': mode,
                    'rows': len(y), 'components': len(set(groups)), 'primary': primary(source, vibe, mode),
                    'metrics': {v: protocol.metrics(y, p, split) for v, p in scores.items()}, 'paired': paired}
        print(json.dumps({'representation_evaluated': source}), flush=True)
    decisions = {v + '__' + a: advancement(v, tables, a + '__vs__' + baseline)
                 for v in models.VIBES for a in plan['new_variants']}
    resources = {'extraction': load(directory / 'extraction.json')['resources'],
                 'fit': load(directory / 'fit-trace.json')['resources'],
                 'evaluation_seconds': time.monotonic() - started}
    failures = []
    if resources['fit']['seconds'] + resources['evaluation_seconds'] > 3600:
        failures.append('Fit/evaluation time exceeded')
    if resources['fit']['peak_rss_bytes'] > 4 * 1024**3:
        failures.append('Fit RSS exceeded')
    for result in decisions.values():
        if failures:
            result['recommendation'] = 'retain'
            result['reasons'].extend(failures)
    save(directory / 'report.json', {'created_at': stamp(), 'decision': decisions, 'tables': tables,
        'native': native, 'resources': resources, 'resource_failures': failures,
        'scope': {'rows': len(rows), 'chunks': sum(len(r['chunk_starts']) for r in rows),
                  'components': len({r['component'] for r in rows}), 'new_memo_used': 0},
        'limitations': ['Previously inspected development cohort; no independent confirmation.',
            'Unknown encoder pretraining overlap and artist aliases.',
            'CPU feature study; browser deployment and subjective listening benefits unmeasured.',
            'Exact adopted controls are exposed diagnostics; causal comparisons use matched nested heads.']})
    evidence.seal(directory, 'evaluation', [directory / 'report.json'])
    print(json.dumps({'decisions': decisions, 'resource_failures': failures}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'extract', 'reuse-extraction', 'fit', 'evaluate', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--extraction-source', type=Path)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory, args.extraction_source)
        elif args.action == 'verify':
            print(json.dumps(evidence.verify(directory)))
        else:
            evidence.validate(directory, execution=True)
            {'extract': extract, 'reuse-extraction': reuse_extraction,
             'fit': fit, 'evaluate': evaluate}[args.action](directory)


if __name__ == '__main__':
    main()
