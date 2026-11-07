"""Compare the exact configured product artifact on frozen external references.

No model fitting or outcome-based selection. Live labels are inventoried through
the existing read-only export; they do not select models or enter these metrics.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil

import numpy as np
from threadpoolctl import threadpool_limits

from ..studies import candidate_followup as followup
from ..studies import mapped_vibe_round as engine
from ..support.external_experiment import LAB, digest, save, stamp
from ..studies.mapped_vibe_inference import replay
from .mapped_vibe_metrics import summary
from .transfer_identity import exclusion
from ..studies.vibe_specialist_round import input_arrays, inside, load

DEFAULT = LAB / 'local/mapped-vibes-20261029/product-baseline-1'


def predict(model, x):
    values = x
    for key in ('native', 'calibration'):
        stage = model[key]
        values = replay({'kind': 'ridge', 'medians': stage['mean'], **stage}, values)
    values = np.maximum(values, 1e-8)
    return values / values.sum(axis=1, keepdims=True)


def run(directory, database):
    from yt_playlist.rec import audio_vibe_model, audio_vibe_external
    from ..training import train_vibes
    from .external_transfer import SNAPSHOT

    if directory.exists():
        raise ValueError('Preserve previous evidence and use a new --directory')
    configured = audio_vibe_model.configured_path()
    if not configured:
        raise ValueError('Configured product model is disabled; cannot call bundled model the active baseline')
    model_path = Path(configured).resolve()
    model_hash = digest(model_path)
    model = audio_vibe_external.validate(json.loads(model_path.read_text()))
    directory.mkdir(parents=True)
    shutil.copyfile(model_path, directory / 'product-model.json')
    original = load(engine.DEFAULT / 'plan.json')
    candidate = load(followup.DEFAULT / 'plan.json')
    references = {str(model_path): model_hash,
                  str(engine.DEFAULT / 'plan.json'): digest(engine.DEFAULT / 'plan.json'),
                  str(followup.DEFAULT / 'plan.json'): digest(followup.DEFAULT / 'plan.json')}
    # Model identity/cohort details are read before any live label export.
    historical = load(SNAPSHOT)
    product_local = historical['gold']
    native = load(engine.DEFAULT / 'inputs/emotify-artist.json')['rows']
    product_training = [*native, *[{**r, 'id': r['video_id']} for r in product_local]]
    known_prior = load(engine.PRIOR / 'inputs/local.json')['protected_identities']
    prior_ids = {r['video_id'] for r in known_prior}
    references[str(SNAPSHOT)] = digest(SNAPSHOT)
    save(directory / 'plan.json', {'created_at': stamp(), 'configured_product_path': str(model_path),
         'product_sha256': model_hash, 'product_training': model['training'],
         'primary': 'fixed product scores and frozen candidate predictions on identical external rows; known product-training identity matches removed in primary slice',
         'scope': 'three audio-backed pilots, soundtrack activation/aggression, direct CAL500 Focus; no fits or local outcome scoring',
         'code_sha256': digest(__file__), 'baseline_aggregation': 'mean embedding then the exact shipped native/calibration/clipping/normalization stages',
         'candidate_aggregation': candidate['primary_pilot_aggregation'],
         'limits': ['Product baseline scores compete across vibes while candidates are independent; absolute-score MSE alone cannot establish product benefit.',
                    'Known identity filtering cannot resolve missing artists, aliases or upstream backbone overlap.',
                    'Pilot datasets were previously inspected. Live local labels are inventory only.']})
    cases = {}
    for target in (*followup.PILOTS, 'soundtracks-all', 'cal500-raw-activity'):
        origin = followup.DEFAULT if target in followup.PILOTS else engine.DEFAULT
        meta_path = origin / f'inputs/{target}.json'
        array_path = origin / f'inputs/{target}.npz'
        expected = candidate if target in followup.PILOTS else original
        metadata = load(meta_path, expected['inputs'][f'inputs/{target}.json'])
        if digest(array_path) != expected['inputs'][f'inputs/{target}.npz']:
            raise ValueError('Frozen evaluation arrays changed')
        references.update({str(meta_path): digest(meta_path), str(array_path): digest(array_path)})
        data = input_arrays(array_path)
        x = followup.average_chunks(data['embedding'], data['offsets']) if target in followup.PILOTS else data['embedding']
        scores = predict(model, x)
        # Independent scalar implementation from the actual product module.
        for i in sorted({0, len(x) // 2, len(x) - 1}):
            emotion = audio_vibe_external._predict(model['native'], x[i].tolist())
            scalar = np.maximum(audio_vibe_external._predict(model['calibration'], emotion), 1e-8)
            if not np.allclose(scores[i], scalar / scalar.sum(), atol=1e-12, rtol=0):
                raise ValueError('Independent exact-product replay mismatch')
        keep, overlap = exclusion(metadata['rows'], product_training)
        if keep.sum() < 10:
            raise ValueError('Insufficient product-disjoint evaluation rows')
        predictions, observed = {}, {}
        if target in followup.PILOTS:
            vibes = ('cruise', 'energize')
            for source in ('pmemo', 'deam', 'emotify-artist'):
                path = followup.DEFAULT / f'pilots-{source}.predictions.npz'
                saved = input_arrays(path)
                references[str(path)] = digest(path)
                for vibe in vibes:
                    observed[vibe] = saved[target + '__' + vibe + '__observed']
                    predictions.setdefault(vibe, {})[source] = saved[target + '__mean_native_then_map__' + vibe + '__selected']
            for vibe in vibes:
                predictions[vibe]['equal_va'] = (predictions[vibe]['pmemo'] + predictions[vibe]['deam']) / 2
        elif target == 'soundtracks-all':
            for source in followup.SOURCE_SETS['soundtracks']:
                path = followup.DEFAULT / f'soundtracks-{source}.predictions.npz'
                saved = input_arrays(path)
                references[str(path)] = digest(path)
                for vibe in ('energize', 'release'):
                    key = target + '__aligned_excerpt__' + vibe + '__selected'
                    if key in saved:
                        observed[vibe] = saved[target + '__' + vibe + '__observed']
                        predictions.setdefault(vibe, {})[source] = saved[key]
        else:
            path = engine.DEFAULT / 'cal500-raw-activity.oof.npz'
            saved = input_arrays(path)
            references[str(path)] = digest(path)
            observed['concentrate'] = saved['concentrate__observed']
            predictions['concentrate'] = {method: saved['concentrate__' + method] for method in
                                          ('prior_embedding_mae', 'selected', 'shared', 'native_mse')}
        result = {'rows': len(x), 'product_disjoint_rows': int(keep.sum()), 'excluded': overlap, 'slices': {}}
        for slice_name, mask in (('all_frozen_rows', np.ones(len(x), dtype=bool)), ('product_disjoint', keep)):
            result['slices'][slice_name] = {}
            for vibe in observed:
                values = {'product': scores[:, model['labels'].index(vibe)], **predictions[vibe]}
                result['slices'][slice_name][vibe] = {name: summary(observed[vibe][mask], p[mask], data['outer'][mask]) for name, p in values.items()}
        cases[target] = result
    # Inventory only: do not score or publish the new local outcomes, and do not
    # describe a previously observed identity as a new independent holdout.
    records, skipped = train_vibes.examples_from_db(database, require_embeddings=True)
    rows = [r for r in records if r.get('embedding_signature') == model['embedding_signature']]
    new = [r for r in rows if r['video_id'] not in prior_ids]
    changed = []
    prior_path = engine.PRIOR / 'inputs/local.json'
    inventory = {'captured_at': stamp(), 'database': str(database.resolve()), 'access': 'read-only; no migrations',
                 'usable_labeled_recordings': len(records), 'compatible_with_product_embedding': len(rows),
                 'previous_protected_identity_count': len(prior_ids), 'new_recording_ids': [r['video_id'] for r in new],
                 'new_compatible_recordings': len(new), 'skipped': dict(skipped),
                 'note': 'New identities are not automatically independent artist groups; labels and scores are not exported or evaluated here.'}
    save(directory / 'inventory.json', inventory)
    references[str(prior_path)] = digest(prior_path)
    if digest(model_path) != model_hash:
        raise ValueError('Configured product artifact changed during comparison')
    for path, expected in references.items():
        if digest(path) != expected:
            changed.append(path)
    if changed:
        raise ValueError('Comparison references changed: ' + repr(changed))
    report = {'product_sha256': model_hash, 'product_training': model['training'], 'cases': cases,
              'inventory': inventory, 'references': references, 'local_outcomes_scored': False}
    save(directory / 'report.json', report)
    print(json.dumps({'report': str(directory / 'report.json'), 'product_sha256': model_hash,
                     'inventory': inventory, 'cases': {k: {'rows': v['rows'], 'product_disjoint_rows': v['product_disjoint_rows']} for k,v in cases.items()}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--database', type=Path, default=Path('/home/rfirmin/.local/share/yt-playlist/state.db'))
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        run(inside(args.directory), args.database)


if __name__ == '__main__':
    main()
