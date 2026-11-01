"""Freeze current corpus overlap groups without rewriting the initial snapshot."""
import hashlib
import json
from pathlib import Path

from .external_checkpoint import RUN
from .external_experiment import OUT, digest, save, stamp
from .external_multitask import balanced_folds, connected_groups
from . import external_multitask


def family(name):
    if name.startswith('emotify'):
        return 'emotify-audio'
    if name.startswith('soundtracks'):
        return 'soundtracks'
    if name.startswith(('4q-', 'merge-')):
        return '4q-merge'
    if name.startswith('jamendo-human'):
        return 'jamendo-human'
    return name.removesuffix('-dynamic')


def freeze():
    rows, sources, feature_files = [], {}, {}
    for path in sorted(OUT.glob('*.features.json')):
        source = json.loads(path.read_text())
        if not source.get('rows'):
            continue
        name = path.name.removesuffix('.features.json')
        sources[name] = {'sha256': digest(path), 'rows': len(source['rows']),
                         'errors': len(source.get('errors', [])), 'family': family(name)}
        for row in source['rows']:
            rows.append({**row, 'task': name})
            feature_files.setdefault(row['feature_file'], None)
    for filename in feature_files:
        feature_files[filename] = digest(filename)
    groups = connected_groups(rows)
    split = balanced_folds(rows, groups)
    inputs = {'sources': sources, 'feature_files': feature_files,
              'code_sha256': {str(path): digest(path) for path in
                              (Path(__file__), Path(external_multitask.__file__))}}
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    path = RUN / 'overlaps' / (key + '.json')
    if not path.exists():
        save(path, {'created_at': stamp(), **inputs,
            'rows': [{'dataset': row['task'], 'id': row['id'], 'group': group, 'fold': int(fold)}
                     for row, group, fold in zip(rows, groups, split)],
            'policy': 'Five task-balanced global connected-component folds; source groups, every known artist credit, recording IDs and encoded audio hashes bind components.',
            'families': 'Correlated versions are alternative experts; their shared recordings never become independent observations.',
            'limitations': ['Unknown artist identities and aliases prevent a complete unseen-artist claim.',
                            'Encoded hashes cannot identify re-encoded audio, edits or different masters.',
                            'Previously saved native reports keep their own explicitly recorded splits.']})
    index = {'created_at': stamp(), 'manifest': str(path), 'sha256': digest(path),
             'rows': len(rows), 'sources': len(sources), 'global_groups': len(set(groups))}
    save(RUN / 'current-overlaps.json', index)
    print(json.dumps({'overlap_manifest': index}), flush=True)
    return index
