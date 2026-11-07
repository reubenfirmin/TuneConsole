"""Bounded source recovery and full-recipe chroma confirmation diagnostics (#815)."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import resource
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

import numpy as np
from threadpoolctl import threadpool_limits

from .. import source_path
from ..studies import individual_protocol as protocol
from ..support import quality_round as evidence
from ..support.external_experiment import DATA, LAB, digest, pcm, save, stamp
from .focus_count_experiment import native_interval
from .focus_experiment import arrays, check_receipt, head_predictions, inside, load
from .focus_mapping import split
from ..studies.individual_experiment import compact
from ..audio.quality_reference_audio import response

ROOT = LAB / 'local/focus-confirmation-815-20261101'
SOURCES = ROOT / 'sources'
DEFAULT = ROOT / 'round-1'
PREVIOUS = LAB / 'local/focus-615-20261030/round-1'
COUNTS = LAB / 'local/focus-counts-711-20261101/round-1'
CHROMA = LAB / 'local/chroma-810-20261101/round-1'
ENVIRONMENT = LAB / 'local/chroma-810'
PYTHON = ENVIRONMENT / 'venv/bin/python'
WORKER = source_path('chroma_worker.py')
METHODS = ('ridge', 'chroma', 'combined', 'training_mean', 'adopted_full')
DOWNLOADS = (
    ('HumMusQA.csv', 'https://zenodo.org/records/18462524/files/HumMusQA.csv?download=1',
     'a5c00b4d5135d403a6c0e22c8be7808c'),
    ('metadata.csv', 'https://zenodo.org/records/18462524/files/metadata.csv?download=1',
     '55b6d6ac5437b05a727301e5bed48d16'),
    ('pepmusic.html', 'https://social-dynamics.net/pepmusic/', None),
    ('music-in-use.json', 'https://api.osf.io/v2/nodes/n3zmp/files/osfstorage/?page%5Bsize%5D=100', None),
    ('brain-focus-redirect.html', 'https://bit.ly/3HbZd8n', None),
    ('7digital-148-preview.bin', 'https://previews.7digital.com/clip/148.mp3', None),
)


def acquire():
    """Read public releases only; no forms, access requests or user listening data."""
    SOURCES.mkdir(parents=True, exist_ok=True)
    receipt = SOURCES / 'acquisition.json'
    if receipt.exists():
        print(json.dumps(load(receipt)), flush=True)
        return
    records = []
    for name, url, md5 in DOWNLOADS:
        record = {'name': name, 'url': url, 'published_md5': md5, 'started_at': stamp()}
        started = time.monotonic()
        limit = 4096 if name.endswith('.bin') else 2 * 1024**2
        try:
            request = Request(url, headers={'User-Agent': 'TuneConsole-research/815',
                **({'Range': 'bytes=0-4095'} if name.endswith('.bin') else {})})
            with urlopen(request, timeout=20) as stream:
                content = stream.read(limit + 1)
                record.update(final_url=stream.url, status=stream.status,
                              content_type=stream.headers.get('Content-Type'))
            if len(content) > limit and not name.endswith('.bin'):
                raise ValueError('Metadata size cap exceeded')
            content = content[:limit]
            if md5 and hashlib.md5(content).hexdigest() != md5:
                raise ValueError('Publisher MD5 mismatch')
            path = SOURCES / name
            path.write_bytes(content)
            record.update(bytes=len(content), sha256=digest(path), acquired=True,
                          limited_probe=name.endswith('.bin'))
        except (URLError, TimeoutError, OSError, ValueError) as error:
            record.update(acquired=False, error=str(error))
        record['seconds'] = time.monotonic() - started
        records.append(record)
        print(json.dumps(record), flush=True)
    save(receipt, {'created_at': stamp(), 'records': records,
        'limits': 'Metadata bounded at 2 MiB per URL and 20 second socket timeout. '
                  '7digital is a 4096-byte public availability probe, not verified rated audio.'})


def audit_sources():
    acquisition = load(SOURCES / 'acquisition.json')
    for record in acquisition['records']:
        if record['acquired'] and digest(SOURCES / record['name']) != record['sha256']:
            raise ValueError('Acquired source changed')
    report = {'created_at': stamp(), 'acquisition': acquisition, 'hummusqa': None,
              'independent_confirmation_rows': 0,
              'notice': 'Eligibility audit only. Unasked activities are unknown; distractors '
                        'are not direct listener suitability votes. No model outcomes used.'}
    if all((SOURCES / p).exists() for p in ('HumMusQA.csv', 'metadata.csv')):
        questions = list(csv.DictReader(io.StringIO((SOURCES / 'HumMusQA.csv').read_text(encoding='utf-8-sig'))))
        metadata = list(csv.DictReader(io.StringIO((SOURCES / 'metadata.csv').read_text(encoding='utf-8-sig'))))
        pattern = re.compile(r'\b(read(?:ing)?|study(?:ing)?|studying|concentrat\w*|focus\w*|homework|work(?:ing)?)\b', re.I)
        matches = [{'index': i, **r} for i, r in enumerate(questions)
                   if pattern.search(' '.join(r.get(c, '') for c in
                       ('Question', 'True answer', 'Distractor 1', 'Distractor 2', 'Distractor 3')))]
        functional = [{'index': i, **r} for i, r in enumerate(questions)
                      if 'functional' in (r.get('Main Category', '') + r.get('Secondary Categories', '')).lower()]
        rows = [{'id': 'hummusqa:' + r['track_id'], 'artist': r['artist_name'],
                 'title': r['name'], 'source_url': r['song_link']} for r in metadata]
        prior = load(DATA / 'discovery/focus-576/reference-audit.json')
        inventories = set(prior['input_features'])
        inventories.update(str(p) for p in (PREVIOUS / 'inputs').glob('*.json') if p.name != 'audit.json')
        exposed = [r for p in sorted(inventories) for r in load(p)['rows']]
        keep, overlap = protocol.exclude(rows, exposed)
        candidates = [r for r in rows if r['source_url'].endswith('/26435')]
        candidate_keys = set().union(*(protocol.keys(r, True) for r in candidates))
        candidate_matches = [{'inventory': p, 'id': old['id'], 'artist': old.get('artist'),
            'title': old.get('title'), 'matching_keys': sorted(protocol.keys(old, True) & candidate_keys)}
            for p in sorted(inventories) for old in load(p)['rows']
            if protocol.keys(old, True) & candidate_keys]
        report['hummusqa'] = {'questions': len(questions), 'tracks': len(metadata),
            'functional_context': functional, 'term_matches': matches,
            'prior_inventories': {p: digest(p) for p in sorted(inventories)},
            'overlap_with_prior_development': overlap, 'unmatched_tracks': int(keep.sum()),
            'study_work_candidates': candidates, 'study_work_overlap': candidate_matches,
            'disposition': 'Manual inspection of all 17 Functional Context questions and all '
                'term matches found one positive studying/working context, question index 225. '
                'Its artist Celestial Aeon Project appears in prior ManyMusic development data. '
                'The other tracks have no direct Reading/Studying labels. No independent '
                'rankable reference set is established; no audio recovery or scoring is justified.'}
    report['other_leads'] = {
        'pepmusic': {'paper': 'https://doi.org/10.1140/epjds/s13688-020-0221-9',
            'disposition': 'Public project page supplies a data-request form, not a public dataset '
                'link. No form submitted. The human study rates three broad activity groups; '
                'studying is bundled with dining, commuting, driving and breakfast. '
                'The 1107-track playlist-derived corpus is not direct listener suitability.'},
        'music_in_use': {'disposition': 'Public OSF files endpoint returned HTTP 401. '
            'Transcription derail timestamps are not general per-song Reading/Studying votes.'},
        'brain_focus': {'repository_tree_sha': '43a5c5e76a2b7c65490ee0d88347cf4fcd44ddb9',
            'repository': 'https://github.com/arctop/Measuring-And-Modeling-The-Effect-of-Audio-On-Human-Focus-In-Everyday-Environments-Using-BCI',
            'disposition': 'Resolved the paper short link. Inspected the complete five-file '
                'Git tree and README with gh api. Data is a 42244658-byte Git LFS object '
                '(sha256 66b3985e9ee8f7d7beb3b37b02e73108fe00131b4ffcb292f5ec4373f0f9c413); '
                'README describes 51 participants with session-level decoded focus and '
                'self-reports across four audio-stream conditions. No stimulus audio is '
                'in the tree. Do not convert model-derived focus dynamics or session '
                'ratings into per-recording human suitability labels.'},
        'moods_activities': {'disposition': 'Retained #576 listened-example annotations remain '
            'promising, but exact original 7digital recording/window matching is unverified. '
            'A single bounded public clip probe returned HTTP 400; this does not establish '
            'that all public recovery is impossible. The intellectual target also includes writing.'}}
    save(SOURCES / 'audit.json', report)
    print(json.dumps({'audit': str(SOURCES / 'audit.json'),
        'questions': report['hummusqa']['questions'] if report['hummusqa'] else 0,
        'independent_confirmation_rows': report['independent_confirmation_rows'],
        'hummusqa_disposition': report['hummusqa']['disposition'] if report['hummusqa'] else 'Not acquired'}), flush=True)


def freeze(directory):
    for f in range(5):
        check_receipt(PREVIOUS, f'outer-{f}.receipt.json')
    evidence.verify(COUNTS)
    check_receipt(CHROMA, 'extraction.receipt.json')
    d = arrays(PREVIOUS / 'inputs/cal500-raw-activity.npz')
    meta = load(PREVIOUS / 'inputs/cal500-raw-activity.json')
    protocol.group_guard(d['groups'], d['outer'])
    if len(d['ids']) != 494 or int(d['common'].sum()) != 450:
        raise ValueError('Accepted cohort changed')
    adopted = LAB.parent.parent / 'src/yt_playlist/rec/data/focus-vibes.json'
    if load(adopted)['heads'] != load(PREVIOUS / 'exports/direct.json')['heads']:
        raise ValueError('Incumbent no longer matches accepted full recipe')
    rows = []
    for row in meta['rows']:
        audio = DATA / 'cal500/audio/CAL500_32kps' / (row['id'].split(':', 1)[1] + '.mp3')
        if digest(audio) != row['audio_sha256']:
            raise ValueError('CAL500 recording identity changed')
        rows.append({**row, 'audio': str(audio)})
    old_rows = load(CHROMA / 'inputs/baseline-rows.json')['rows']
    old_ids = {r['id'] for r in old_rows if r['source'] == 'cal500-raw-activity'}
    old_mask = np.isin(d['ids'], sorted(old_ids))
    prior_groups = set(d['groups'][old_mask])
    inputs = {'cal.npz': PREVIOUS / 'inputs/cal500-raw-activity.npz',
        'metadata.json': PREVIOUS / 'inputs/cal500-raw-activity.json', 'adopted.json': adopted,
        'counts.npz': COUNTS / 'inputs/counts.npz', 'count-audit.json': COUNTS / 'inputs/count-audit.json',
        'source-audit.json': SOURCES / 'audit.json', 'acquisition.json': SOURCES / 'acquisition.json',
        **{f'prior-outer-{f}.npz': PREVIOUS / f'outer-{f}.npz' for f in range(5)}}
    refs = [PREVIOUS / 'plan.json', COUNTS / 'plan.json', CHROMA / 'plan.json',
            CHROMA / 'features.npz', CHROMA / 'inputs/baseline-rows.json',
            ENVIRONMENT / 'requirements.lock', *[Path(r['audio']) for r in rows]]
    refs.extend(SOURCES / r['name'] for r in load(SOURCES / 'acquisition.json')['records'] if r['acquired'])
    refs.extend(Path(p) for p in load(SOURCES / 'audit.json')['hummusqa']['prior_inventories'])
    if load(SOURCES / 'audit.json')['independent_confirmation_rows'] != 0:
        raise ValueError('This frozen development diagnostic requires a new plan for independent references')
    evidence.freeze(directory, 815, {
        'outer_folds': 5, 'inner_folds': 3, 'bootstrap_draws': 1000,
        'native_rows': 494, 'common_rows': 450, 'methods': METHODS,
        'chroma_dimensions': 72, 'old_chroma_ids': sorted(old_ids),
        'old_chroma_groups': sorted(int(g) for g in prior_groups),
        'independent_confirmation_rows': 0,
        'decision': {'ndcg_gain': .02, 'top_lift_gain': .01, 'native_mse_regression': .01,
                     'adopted_full_ranking_regression': .01},
        'budgets': {'extract_seconds': 1800, 'fit_evaluate_seconds': 1800,
                    'rss_bytes': 2 * 1024**3, 'feature_bytes': 8 * 1024**2}},
        inputs, references=refs, documents=[WORKER,
            LAB.parent / 'investigations/focus_confirmation_815.py'])
    # Derived identities are frozen before extraction or outcome evaluation.
    save(directory / 'inputs/rows.json', rows)
    plan = load(directory / 'plan.json')
    plan['inputs']['inputs/rows.json'] = digest(directory / 'inputs/rows.json')
    save(directory / 'plan.json', plan)
    evidence.validate(directory, True)
    print(json.dumps({'plan_sha256': digest(directory / 'plan.json'),
                      'rows': len(rows), 'old_chroma_rows': int(old_mask.sum())}), flush=True)


def extract(directory):
    plan = evidence.validate(directory, True)
    if (directory / 'extraction.receipt.json').exists():
        check_receipt(directory, 'extraction.receipt.json')
        return
    rows = load(directory / 'inputs/rows.json')
    old_rows = load(CHROMA / 'inputs/baseline-rows.json')['rows']
    old_data = arrays(CHROMA / 'features.npz')['chroma']
    old_lookup = {r['id']: (r, old_data[i, 0]) for i, r in enumerate(old_rows)
                  if r['source'] == 'cal500-raw-activity'}
    values, audit = [], []
    env = {**os.environ, **{k: '1' for k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
            'MKL_NUM_THREADS', 'NUMBA_NUM_THREADS')}, 'NUMBA_CACHE_DIR': str(ENVIRONMENT / 'numba-cache')}
    started = time.monotonic()
    with TemporaryDirectory(prefix='pcm-', dir=directory) as temporary, (directory / 'worker.log').open('w') as log:
        worker = subprocess.Popen([str(PYTHON), '-B', str(WORKER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log, text=True, env=env, bufsize=1)
        try:
            runtime = response(worker, 'READY ')
            for i, row in enumerate(rows):
                if time.monotonic() - started > plan['protocol']['budgets']['extract_seconds']:
                    raise RuntimeError('Extraction budget exceeded')
                signal = Path(temporary) / 'interval.f32'
                pcm(row['audio'], row['sample_start'], 16000, 1, signal)
                worker.stdin.write(json.dumps({'id': row['id'], 'pcm': str(signal)}) + '\n')
                worker.stdin.flush()
                result = response(worker, 'RESULT ')
                if result['id'] != row['id']:
                    raise ValueError('Chroma worker identity changed')
                vector = np.asarray(result.pop('embedding'))
                old_error = None
                if row['id'] in old_lookup:
                    old, reference = old_lookup[row['id']]
                    if old['audio_sha256'] != row['audio_sha256'] or old['chunk_starts'][0] != row['sample_start']:
                        raise ValueError('Prior chroma interval differs')
                    old_error = float(np.max(np.abs(vector - reference)))
                    if old_error > 1e-12:
                        raise ValueError('Prior chroma feature replay failed')
                values.append(vector)
                audit.append({**result, 'start': row['sample_start'], 'pcm_sha256': digest(signal),
                              'old_feature_maximum_error': old_error})
                if (i + 1) % 50 == 0:
                    print(json.dumps({'extracted': i + 1}), flush=True)
        finally:
            worker.stdin.close()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)
            worker.stdout.close()
    path = directory / 'chroma.npz'
    np.savez_compressed(path, chroma=np.asarray(values), ids=np.asarray([r['id'] for r in rows]))
    if path.stat().st_size > plan['protocol']['budgets']['feature_bytes']:
        raise RuntimeError('Feature byte budget exceeded')
    save(directory / 'extraction.json', {'runtime': runtime, 'rows': audit,
        'seconds': time.monotonic() - started, 'feature_bytes': path.stat().st_size,
        'peak_worker_rss_bytes': max(r['peak_rss_bytes'] for r in audit)})
    evidence.seal(directory, 'extraction', [path, directory / 'extraction.json'])


def run(directory):
    plan = evidence.validate(directory, True)
    check_receipt(directory, 'extraction.receipt.json')
    d, c = arrays(directory / 'inputs/cal.npz'), arrays(directory / 'chroma.npz')
    if not np.array_equal(d['ids'], c['ids']):
        raise ValueError('Feature identities differ')
    variants = {'ridge': d['x'], 'chroma': c['chroma'],
                'combined': np.column_stack([d['x'], c['chroma']])}
    started = time.monotonic()
    for fold in range(5):
        stage = f'outer-{fold}'
        if (directory / (stage + '.receipt.json')).exists():
            check_receipt(directory, stage + '.receipt.json')
            continue
        train, valid = d['outer'] != fold, d['outer'] == fold
        inner = split(d['groups'][train])
        models, traces, predictions = {}, {}, {'ids': d['ids'][valid]}
        for name, x in variants.items():
            models[name], traces[name] = protocol.select(x[train], d['y'][train], inner)
            predictions[name] = head_predictions(models[name], x[valid], True)
        models['training_mean'] = d['y'][train].mean(0).tolist()
        predictions['training_mean'] = np.broadcast_to(models['training_mean'], (sum(valid), 2))
        predictions['adopted_full'] = head_predictions(load(directory / 'inputs/adopted.json')['heads'], d['x'][valid], True)
        old = arrays(directory / f'inputs/prior-outer-{fold}.npz')
        if not np.array_equal(old['ids'], predictions['ids']) or not np.allclose(
                old['historical__direct'], predictions['ridge'], rtol=0, atol=1e-12):
            raise ValueError('Full incumbent recipe does not replay accepted protected folds')
        traces.update(training_ids=d['ids'][train].tolist(), training_groups=d['groups'][train].tolist(),
            inner_folds=inner.tolist(), validation_ids=d['ids'][valid].tolist(),
            validation_groups=d['groups'][valid].tolist())
        paths = [directory / (stage + suffix) for suffix in ('.model.json', '.trace.json', '.npz')]
        compact(paths[0], models)
        compact(paths[1], traces)
        np.savez_compressed(paths[2], **predictions)
        evidence.seal(directory, stage, paths)
        print(json.dumps({'completed': stage}), flush=True)
        if time.monotonic() - started > plan['protocol']['budgets']['fit_evaluate_seconds']:
            raise RuntimeError('Fit budget exceeded')
    evaluate(directory, plan, time.monotonic() - started)


def evaluate(directory, plan, fit_seconds):
    d = arrays(directory / 'inputs/cal.npz')
    predictions = {m: np.empty_like(d['y']) for m in METHODS}
    for fold in range(5):
        check_receipt(directory, f'outer-{fold}.receipt.json')
        v, valid = arrays(directory / f'outer-{fold}.npz'), d['outer'] == fold
        if not np.array_equal(v['ids'], d['ids'][valid]):
            raise ValueError('Saved prediction identity differs')
        for method in METHODS:
            predictions[method][valid] = v[method]
    cohorts = {'common450': d['common'], 'all494': np.ones(len(d['y']), dtype=bool),
        'outside_prior_chroma_components': ~np.isin(d['groups'], plan['protocol']['old_chroma_groups'])}
    started = time.monotonic()
    report = {'created_at': stamp(), 'cohorts': {}, 'controls': {
        'ridge': 'Complete incumbent procedure, protected outer and inner refits; replays #615 within 1e-12.',
        'adopted_full': 'Exact installed heads trained on this corpus. Exposed replay only.'},
        'independent_confirmation_rows': plan['protocol']['independent_confirmation_rows'],
        'new_memo_used': 0, 'browser_chroma_parity_established': False}
    for name, mask in cohorts.items():
        y, outer, groups = d['y'][mask], d['outer'][mask], d['groups'][mask]
        p = {m: v[mask] for m, v in predictions.items()}
        scores = {m: v.mean(1) for m, v in p.items()}
        pairs = [('combined', control) for control in ('ridge', 'training_mean', 'adopted_full')]
        result = {'rows': int(mask.sum()), 'groups': len(set(groups)),
            'focus': {m: protocol.metrics(y.mean(1), v, outer) for m, v in scores.items()},
            'paired_focus': protocol.paired(y.mean(1), scores, outer, groups, pairs, 1000),
            'native': {m: {'mse': ((v-y)**2).mean(0).tolist(), 'tasks': {
                t: protocol.metrics(y[:, j], v[:, j], outer) for j, t in enumerate(('reading', 'studying'))}}
                for m, v in p.items()},
            'paired_native_mse': native_interval((p['combined']-y)**2-(p['ridge']-y)**2, groups, outer),
            'paired_native_ranking': {t: protocol.paired(y[:, j], {m: v[:, j] for m, v in p.items()},
                outer, groups, [('combined', 'ridge')], 1000) for j, t in enumerate(('reading', 'studying'))}}
        report['cohorts'][name] = result
    r = report['cohorts']['common450']
    pair, exposed = r['paired_focus']['combined__vs__ridge'], r['paired_focus']['combined__vs__adopted_full']
    checks = {'ndcg_gain_at_least_002': pair['ndcg']['difference'] >= .02,
        'top_lift_gain_at_least_001': pair['top_lift']['difference'] >= .01,
        'positive_conditional_ndcg_lower_bound': pair['ndcg']['conditional_95_percent_interval'][0] > 0,
        'native_mse_regression_at_most_001': max(np.asarray(r['native']['combined']['mse']) - r['native']['ridge']['mse']) <= .01,
        'exposed_full_ndcg_regression_at_most_001': exposed['ndcg']['difference'] >= -.01,
        'exposed_full_lift_regression_at_most_001': exposed['top_lift']['difference'] >= -.01}
    report['decision'] = {'development_checks': {k: bool(v) for k, v in checks.items()},
        'development_pass': bool(all(checks.values())), 'independent_confirmation_pass': False,
        'productionize': False, 'conclusion': 'Retain incumbent; independent confirmation is missing.',
        'limits': 'All CAL500 identities and labels are development-exposed, including rows outside '
            'the earlier chroma pilot. Whole-song votes over ten-second excerpts; listeners mostly '
            'unidentified. Intervals condition on these folds and fits. No attention-benefit claim.'}
    report['resources'] = {'fit_seconds': fit_seconds, 'evaluation_seconds': time.monotonic()-started,
        'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'outer_model_bytes': sum(p.stat().st_size for p in directory.glob('outer-*.model.json'))}
    if report['resources']['peak_rss_bytes'] > plan['protocol']['budgets']['rss_bytes']:
        raise RuntimeError('RSS budget exceeded')
    if fit_seconds + report['resources']['evaluation_seconds'] > plan['protocol']['budgets']['fit_evaluate_seconds']:
        raise RuntimeError('Fit/evaluation wall-clock budget exceeded')
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', ids=d['ids'], groups=d['groups'],
                        outer=d['outer'], observed=d['y'], **predictions)
    evidence.seal(directory, 'evaluation', [directory / 'report.json', directory / 'predictions.npz'])
    print(json.dumps({'decision': report['decision'], 'resources': report['resources']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['acquire', 'audit-sources', 'freeze', 'extract', 'run', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'acquire':
            acquire()
        elif args.action == 'audit-sources':
            audit_sources()
        elif args.action == 'freeze':
            freeze(directory)
        elif args.action == 'extract':
            extract(directory)
        elif args.action == 'run':
            run(directory)
        else:
            print(json.dumps(evidence.verify(directory,
                required_stages=['extraction', *[f'outer-{f}' for f in range(5)], 'evaluation'])))


if __name__ == '__main__':
    main()
