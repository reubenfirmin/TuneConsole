"""Official MERGE audio, with listener-checked classes and tag-derived VA separate."""
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import zipfile

from .external_experiment import DATA, OUT, digest, save, stamp
from .external_secondary import identity

BASE = DATA / 'merge'
ARCHIVE_MD5 = '167f7ba80ab8eca8eee5a8668498a419'
QUADRANTS = ('high_positive', 'high_negative', 'low_negative', 'low_positive')


def stage():
    archive = BASE / 'MERGE_Audio_Complete.zip'
    partial = BASE / 'MERGE_Audio_Complete.zip.partial'
    if not archive.exists():
        if not partial.exists() or partial.stat().st_size != 1162604519:
            raise ValueError('Official MERGE archive download is incomplete')
        with partial.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'md5').hexdigest()
        if actual != ARCHIVE_MD5:
            raise ValueError('Official MERGE archive MD5 mismatch')
        partial.replace(archive)
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'md5').hexdigest() != ARCHIVE_MD5:
            raise ValueError('Official MERGE archive changed')
    archive_hash = digest(archive)
    marker = BASE / 'acquisition.json'
    if marker.exists():
        previous = json.loads(marker.read_text())
        if previous.get('archive_sha256') == archive_hash and all(
                (BASE / 'source' / name).is_file() for name in previous['members']):
            return previous
    destination = BASE / 'source'
    members = {}
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            relative = Path(info.filename)
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'MERGE_Audio_Complete':
                raise ValueError('Unsafe MERGE archive path')
            if info.is_dir():
                continue
            if relative.suffix.lower() not in ('.mp3', '.csv'):
                raise ValueError('Unexpected MERGE archive member: ' + info.filename)
            # read() also checks each member's CRC against the central directory.
            data = zipped.read(info)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            members[info.filename] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    report = {'created_at': stamp(), 'source': 'https://zenodo.org/records/13939205',
              'version': '1.1.0', 'published_md5': ARCHIVE_MD5, 'archive_sha256': archive_hash,
              'members': members, 'audio_files': sum(name.lower().endswith('.mp3') for name in members)}
    if report['audio_files'] != 3554:
        raise ValueError('Expected 3554 supplied MERGE audio clips')
    save(marker, report)
    return report


def prepare_rows(task='quadrants'):
    if task not in ('quadrants', 'tag-va'):
        raise ValueError('Unknown MERGE native task')
    root = BASE / 'source/MERGE_Audio_Complete'
    metadata_path = root / 'merge_audio_complete_metadata.csv'
    av_path = root / 'merge_audio_complete_av_values.csv'
    metadata = list(csv.DictReader(metadata_path.open(encoding='utf-8-sig')))
    av = list(csv.DictReader(av_path.open(encoding='utf-8-sig')))
    targets = {row['Song']: [float(row[key]) for key in ('Valence', 'Arousal')] for row in av}
    if len(metadata) != 3554 or len(targets) != 3554 or {row['Song'] for row in metadata} != set(targets):
        raise ValueError('MERGE metadata/VA identity alignment changed')
    if any(not 0 <= value <= 1 for values in targets.values() for value in values):
        raise ValueError('MERGE release VA values are expected already normalized to [0,1]')
    parents = {}

    def root_group(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = root_group(parents[key])
        return parents[key]

    # Artist, exact song identity, and repeated AllMusic IDs all bind folds.
    identities = {}
    rows = []
    for row in metadata:
        identifier, quadrant = row['Song'], row['Quadrant']
        if quadrant not in ('Q1', 'Q2', 'Q3', 'Q4') or Path(identifier).name != identifier:
            raise ValueError('Invalid MERGE identifier/quadrant')
        audio = root / quadrant / (identifier + '.mp3')
        if not audio.is_file():
            raise ValueError('Missing MERGE audio: ' + identifier)
        artist, title = row['Artist'].strip(), row['Title'].strip()
        group = 'merge:' + (identity(artist) or identifier)
        keys = [('artist_title', identity(artist), identity(title))] if artist and title else []
        if row['AllMusic Id'].strip():
            keys.append(('allmusic', row['AllMusic Id'].strip()))
        for key in keys:
            if key in identities:
                parents[root_group(group)] = root_group(identities[key])
            else:
                identities[key] = group
        rows.append({'id': 'merge:' + identifier, 'audio': str(audio), 'artist': artist, 'title': title,
                     'group': group, 'allmusic_id': row['AllMusic Id'].strip(),
                     'target_names': list(QUADRANTS) if task == 'quadrants' else ['valence', 'arousal'],
                     'targets': [int(quadrant == f'Q{i + 1}') for i in range(4)] if task == 'quadrants' else targets[identifier],
                     'native_scale': [0, 1], 'quadrant': quadrant,
                     'annotation_unit': 'supplied_excerpt_manually_validated_tag_derived_quadrant' if task == 'quadrants'
                         else 'supplied_excerpt_automatically_tag_derived_valence_arousal'})
    for row in rows:
        row['group'] = root_group(row['group'])
    splits = {}
    for path in sorted((root / 'tvt_dataframes').rglob('*.csv')):
        splits[str(path.relative_to(root))] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    existing = {}
    for name in ('4q-emotion', 'coimbra-mirex-like', 'cal500', 'pmemo'):
        path = OUT / f'{name}.features.json'
        if path.exists():
            old = json.loads(path.read_text())['rows']
            old_keys = {(identity(row.get('artist', '')), identity(row.get('title', '')))
                        for row in old if row.get('artist') and row.get('title')}
            old_hashes = {row['audio_sha256'] for row in old}
            existing[name] = {'strict_artist_title_rows': sum(
                (identity(row['artist']), identity(row['title'])) in old_keys for row in rows),
                'identical_encoded_audio_rows': sum(digest(row['audio']) in old_hashes for row in rows)}
    save(OUT / f'merge-{task}.join-audit.json', {
         'created_at': stamp(), 'rows': len(rows), 'groups': len({row['group'] for row in rows}),
         'quadrants': dict(Counter(row['quadrant'] for row in rows)), 'existing_audio_overlap': existing,
         'metadata_sha256': digest(metadata_path), 'av_sha256': digest(av_path), 'provided_splits': splits,
         'policy': 'Exact Song ID joins supplied quadrant directory, metadata and VA table. Fold groups bind artists, identical recording identities and repeated AllMusic IDs; extraction also binds identical encoded audio.',
         'limitations': ['MERGE extends 4Q and replaces it in consensus.',
                        'Quadrants were manually confirmed against tag-derived candidates, rather than assigned through an unrestricted listener panel.',
                        'Continuous VA is automatically derived from emotion tags, not listener-rated.',
                        'The retrieved VA file already uses [0,1]; do not rescale it from the paper\'s canonical [-1,1].',
                        'Provided splits are preserved but the new primary diagnostics use grouped artist folds.']})
    return rows


def rebind_tag_va():
    from .external_experiment import prepare
    rows = {row['id']: row for row in prepare('merge-tag-va')}
    source_path = OUT / 'merge-quadrants.features.json'
    features = json.loads(source_path.read_text())
    rebound = [{**row, **rows[row['id']]} for row in features['rows']]
    save(OUT / 'merge-tag-va.features.json', {**features, 'dataset': 'merge-tag-va', 'rows': rebound,
         'task_provenance': {'source_features_sha256': digest(source_path),
                             'policy': 'Identical audio features; substitute only the separate tag-derived native targets.'}})
    return rebound


def compare_4q():
    """Measure added training audio on an identical held-out 4Q target task."""
    import numpy as np
    from threadpoolctl import threadpool_limits
    from .external_classification import metrics as classification_metrics
    from .external_multitask import connected_groups
    from .external_training import choose_alpha, feature_matrices, fit_path, folds, predict

    with threadpool_limits(limits=1):
        original = json.loads((OUT / '4q-emotion.features.json').read_text())['rows']
        expanded = json.loads((OUT / 'merge-quadrants.features.json').read_text())['rows']
        names = original[0]['target_names']
        if list(names) != list(QUADRANTS) or expanded[0]['target_names'] != list(QUADRANTS):
            raise ValueError('4Q and MERGE target meanings/order do not match')
        all_rows = [{**row, 'task': '4q'} for row in original] + [{**row, 'task': 'merge'} for row in expanded]
        groups = np.asarray(connected_groups(all_rows))
        original_groups, expanded_groups = groups[:len(original)], groups[len(original):]
        split = folds(original_groups.tolist())
        base_x = feature_matrices(original)[0]
        extra_x = feature_matrices(expanded)[0]
        y = np.asarray([row['targets'] for row in original], dtype=float)
        extra_y = np.asarray([row['targets'] for row in expanded], dtype=float)
        methods = {family: {method: np.zeros_like(y) for method in ('4q_only', '4q_plus_merge')}
                   for family in ('attributes', 'embedding', 'attributes_embedding')}
        choices = []
        for fold in range(5):
            train, valid = split != fold, split == fold
            heldout = set(original_groups[valid])
            seen_hashes = {row['audio_sha256'] for row, keep in zip(original, train) if keep}
            retained = []
            for index, (row, group) in enumerate(zip(expanded, expanded_groups)):
                if group in heldout or row['audio_sha256'] in seen_hashes:
                    continue
                retained.append(index)
                seen_hashes.add(row['audio_sha256'])
            if not retained or set(expanded_groups[retained]) & heldout:
                raise ValueError('Invalid MERGE expansion training partition')
            for family, predictions in methods.items():
                for method in predictions:
                    x_train, y_train, training_groups = base_x[family][train], y[train], original_groups[train].tolist()
                    if method == '4q_plus_merge':
                        x_train = np.vstack([x_train, extra_x[family][retained]])
                        y_train = np.vstack([y_train, extra_y[retained]])
                        training_groups += expanded_groups[retained].tolist()
                    alpha = choose_alpha(x_train, y_train, training_groups, list(range(4)))
                    predictions[method][valid] = predict(fit_path(x_train, y_train, [alpha])[0], base_x[family][valid])
                    choices.append({'fold': fold, 'family': family, 'method': method, 'alpha': alpha,
                                    'training_rows': len(y_train), 'heldout_rows': int(valid.sum()),
                                    'added_merge_rows': len(retained) if method == '4q_plus_merge' else 0})
        report = {'created_at': stamp(), 'target_dataset': '4q-emotion', 'rows': len(original),
                  'groups': len(set(original_groups)), 'target_names': names,
                  'methods': {family: {method: classification_metrics(y, values) for method, values in predictions.items()}
                              for family, predictions in methods.items()},
                  'choices_by_fold': choices, 'script_sha256': digest(__file__),
                  'source_features_sha256': {name: digest(OUT / f'{name}.features.json')
                                            for name in ('4q-emotion', 'merge-quadrants')},
                  'validation': 'Five folds on 4Q examples, grouped by global cross-corpus artist/audio connected components. Both methods score the identical held-out 4Q recordings. Expanded training adds MERGE only outside held-out components and deduplicates encoded audio. Three grouped inner folds choose ridge regularization using native one-hot MAE.',
                  'limitations': ['These global folds can differ from the earlier standalone 4Q experiment; compare only the two methods in this report.',
                                 'Unknown artist aliases and distinct encodings can leave unidentified overlaps.',
                                 'MERGE uses manually checked tag-derived classes; this is a native quadrant expansion test, not a new six-vibe evaluation.']}
        save(OUT / 'merge-4q-expansion.report.json', report)
        save(OUT / 'merge-4q-expansion.oof.json', {'ids': [row['id'] for row in original],
             'folds': split.tolist(), 'observed': y.tolist(),
             'predictions': {family: {method: values.tolist() for method, values in predictions.items()}
                             for family, predictions in methods.items()}})
        return report
