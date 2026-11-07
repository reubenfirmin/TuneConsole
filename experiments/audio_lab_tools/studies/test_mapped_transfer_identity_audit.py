"""Reviewer metadata audit; never fit models or open feature/target arrays.

Direct exclusions must reproduce the frozen transfer reports. Connected-source
remainders require an artist/recording/audio anchor; conservative title-only
matches and broader frozen groups are reported separately. Neither an empty
result nor a failure resolves missing or ambiguous supplied identities.
"""
import hashlib
import json
from pathlib import Path
import platform
import time
import unicodedata


ROUND = (Path(__file__).resolve().parents[2]
         / 'audio_lab/local/mapped-vibes-20261029/round-1')
PLAN_SHA256 = '5d8744ca36ce0037d47f756333e26ba5f04198c0faa1a4b56c9f6e51d49a32dc'
VA = ('pmemo', 'deam', 'vgmidi-original', 'merp-static',
      'amg1608-matched', 'ntwicm-matched')
EXPECTED = {(a, b) for a in VA for b in VA if a != b} | {
    ('emotify-artist', 'emma-preview-matched'),
    ('emma-preview-matched', 'emotify-artist'),
}


def _identity(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold()
                   if c.isalnum())


def _artists(value):
    keys = {_identity(value)}
    for credit in value.split('+'):
        keys.add(_identity(credit))
        if credit.count(',') == 1:
            surname, given = credit.split(',')
            keys.add(_identity(given + ' ' + surname))
    return keys - {''}


def _title(value):
    text = unicodedata.normalize('NFKD', value)
    return _identity(''.join(c for c in text if not unicodedata.combining(c)))


def _keys(row, *, include_title=False):
    keys = {('artist', key) for key in _artists(row.get('artist') or '')}
    for field, kind in (('id', 'recording'), ('audio_sha256', 'audio')):
        if row.get(field):
            keys.add((kind, row[field]))
    if include_title and row.get('title'):
        keys.add(('title', _title(row['title'])))
    return keys


def _components(rows):
    """Connect only supplied artist/recording/audio identities, not franchises."""
    parents, first = list(range(len(rows))), {}

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, row in enumerate(rows):
        for key in _keys(row):
            if key in first:
                left, right = root(i), root(first[key])
                parents[max(left, right)] = min(left, right)
            else:
                first[key] = i
    return [root(i) for i in range(len(rows))]


def _direction(source, target):
    rows = source['rows']
    target_keys = set().union(*(_keys(r, include_title=True)
                               for r in target['rows']))
    matches = [sorted(_keys(r, include_title=True) & target_keys) for r in rows]
    direct = {i for i, match in enumerate(matches) if match}
    components = _components(rows)

    def remainders(groups, *, details):
        result = []
        for group in sorted({groups[i] for i in direct}, key=str):
            excluded = [i for i in direct if groups[i] == group]
            retained = [i for i in range(len(rows))
                        if i not in direct and groups[i] == group]
            if retained:
                item = {'group': group,
                        'excluded_ids': sorted(rows[i]['id'] for i in excluded),
                        'retained_ids': sorted(rows[i]['id'] for i in retained)}
                if details:
                    item['excluded_identity_matches'] = [
                        {'id': rows[i]['id'], 'matches': matches[i]} for i in sorted(excluded)]
                    item['retained_metadata'] = [
                        {k: rows[i].get(k) for k in ('id', 'artist', 'title', 'audio_sha256')}
                        for i in retained]
                result.append(item)
        return result

    component_remainders = remainders(components, details=True)
    connected, title_only = [], []
    for component in component_remainders:
        anchors = [item for item in component['excluded_identity_matches']
                   if any(kind != 'title' for kind, _ in item['matches'])]
        component['known_identity_anchors'] = anchors
        (connected if anchors else title_only).append(component)
    return {
        'source_rows': len(rows), 'target_rows': len(target['rows']),
        'direct_excluded_ids': sorted(rows[i]['id'] for i in direct),
        'retained_rows': len(rows) - len(direct),
        'missing_artist_rows': {
            'source': sum(not _artists(r.get('artist') or '') for r in rows),
            'target': sum(not _artists(r.get('artist') or '') for r in target['rows']),
        },
        'connected_identity_remainders': connected,
        'connected_retained_rows': sum(len(r['retained_ids']) for r in connected),
        'title_only_conservative_remainders': title_only,
        'title_only_connected_retained_rows': sum(len(r['retained_ids']) for r in title_only),
        'frozen_group_remainders': remainders(source['groups'], details=False),
    }


def test_connected_identity_audit_distinguishes_coarse_groups():
    source = {'rows': [
        {'id': 'a1', 'artist': 'Artist A', 'audio_sha256': 'same'},
        {'id': 'a2', 'artist': 'Artist A'},
        {'id': 'b', 'artist': 'Artist B'},
        {'id': 'unknown'},
    ], 'groups': ['franchise'] * 4}
    target = {'rows': [{'id': 'target', 'audio_sha256': 'same'}]}
    result = _direction(source, target)
    assert result['direct_excluded_ids'] == ['a1']
    assert result['connected_retained_rows'] == 1
    assert result['connected_identity_remainders'][0]['retained_ids'] == ['a2']
    assert result['frozen_group_remainders'][0]['retained_ids'] == ['a2', 'b', 'unknown']
    assert _direction(source, {'rows': [{'id': 'unrelated'}]})['connected_retained_rows'] == 0
    source['rows'][0]['title'] = 'Shared title'
    title_only = _direction(source, {'rows': [{'id': 'other', 'title': 'Shared title'}]})
    assert title_only['direct_excluded_ids'] == ['a1']
    assert title_only['connected_retained_rows'] == 0
    assert title_only['title_only_connected_retained_rows'] == 1


def test_frozen_transfer_training_excludes_connected_target_identities(tmp_path):
    started = time.perf_counter()
    files, datasets, directions, errors = {}, {}, {}, []

    def read(path, expected=None):
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if expected is not None:
            assert checksum == expected, f'Frozen metadata changed: {path}'
        files[str(path)] = {'sha256': checksum, 'bytes': len(raw)}
        return json.loads(raw)

    plan = read(ROUND / 'plan.json', PLAN_SHA256)
    assert plan['format'] == 'external-attributes-fixed-vibes-v1'
    transfer = [tuple(pair) for pair in plan['transfer']]
    assert len(transfer) == 32 and len(set(transfer)) == 32 and set(transfer) == EXPECTED
    for name in sorted({name for pair in transfer for name in pair}):
        relative = f'inputs/{name}.json'
        metadata = read(ROUND / relative, plan['inputs'][relative])
        assert metadata['dataset'] == name
        assert len(metadata['rows']) == len(metadata['groups']) == plan['datasets'][name]['rows']
        datasets[name] = metadata
    for source, target in transfer:
        key = source + '__' + target
        receipt = read(ROUND / f'transfer-{key}.receipt.json')
        assert receipt['state'] == 'complete' and receipt['fingerprint'] == PLAN_SHA256
        relative = f'transfer-{key}.report.json'
        saved = read(ROUND / relative, receipt['outputs'][relative])
        assert (saved['source'], saved['target']) == (source, target)
        result = _direction(datasets[source], datasets[target])
        result['frozen_state'] = saved['state']
        result['frozen_training_rows'] = saved['training_rows']
        if sorted(saved['excluded_ids']) != result['direct_excluded_ids']:
            errors.append(key + ': independent direct exclusions differ from retained report')
        if saved['training_rows'] != result['retained_rows']:
            errors.append(key + ': retained training row count differs')
        if saved['state'] not in ('complete', 'insufficient_disjoint_training_groups'):
            errors.append(key + ': unexpected frozen transfer state')
        directions[key] = result
    changed = [path for path, before in files.items()
               if hashlib.sha256(Path(path).read_bytes()).hexdigest() != before['sha256']]
    affected = {key: result['connected_retained_rows'] for key, result in directions.items()
                if result['connected_retained_rows'] and result['frozen_state'] == 'complete'}
    report = {
        'scope': 'Independent metadata-only audit; no fitting, extraction, target-array or consumer reads',
        'round': str(ROUND), 'python': platform.python_version(),
        'plan_sha256': files[str(ROUND / 'plan.json')]['sha256'],
        'directions': directions, 'completed_directions_with_connected_remainders': affected,
        'direct_report_mismatches': errors, 'inputs_unchanged': not changed,
        'changed_inputs': changed, 'metadata_files': files,
        'wall_s': time.perf_counter() - started,
        'limits': [
            'Frozen coarse groups may include franchises; their remainders alone are not called identity leakage.',
            'Title-only matches are conservative exclusions, not established recording identities; they do not trigger failure.',
            'Connected remainders require a direct artist/recording/audio anchor and show retained members of that source component.',
            'This stronger connected-component check does not claim a direct target identity remains or establish metric bias.',
            'Missing or ambiguous artist identities, audio aliases, and unknown upstream training overlap remain unresolved.',
            'Skipped directions are reported but do not contribute affected completed results.',
        ],
    }
    output = tmp_path.parent / 'transfer-identity-audit.json'
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(f'Transfer identity audit: {output}; affected completed directions: {affected}')
    assert not changed, changed
    assert not errors, errors
    assert not affected, f'Connected source identities remain in completed transfer fits: {affected}; see {output}'
