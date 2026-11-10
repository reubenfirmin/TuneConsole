"""Bounded provider evidence audit (#585); the account database is read-only."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata

import httpx

LOCAL = Path(__file__).resolve().parents[2] / 'audio_lab/local/provider-585'
FIELDS = {'tempo': 'bpm', 'energy': 'energy', 'danceability': 'danceability',
          'instrumentalness': 'instrumental', 'acousticness': 'mood_acoustic'}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    path.chmod(0o600)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def literal(value):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value or '').casefold()))


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def decide(expected, candidate):
    """Verify a recording only with corroborating accepted IDs and version metadata."""
    if literal(expected['title']) != literal(candidate.get('trackTitle')):
        return 'rejected', 'title_or_version_mismatch'
    if literal(expected['artist']) not in {literal(a.get('name')) for a in candidate.get('artists', [])}:
        return 'rejected', 'artist_mismatch'
    seconds, ms = expected.get('duration_s'), candidate.get('durationMs')
    if not (finite(seconds) and seconds > 0 and finite(ms) and ms > 0):
        return 'ambiguous', 'duration_unavailable'
    if abs(seconds * 1000 - ms) > max(3000, .02 * seconds * 1000):
        return 'rejected', 'duration_mismatch'
    ids = expected.get('identifiers', [])
    corroborated = []
    for scheme, returned in (('isrc', candidate.get('isrc')), ('spotify', candidate.get('href'))):
        supplied = {str(item['value']).strip().casefold() for item in ids
                    if item['scheme'] == scheme and item['status'] == 'accepted'
                    and item['entity_type'] in ('recording', 'track')}
        def identifier(value):
            value = value.strip().casefold()
            return value.rstrip('/').split('/')[-1].split(':')[-1].split('?')[0] if scheme == 'spotify' else value.replace('-', '')
        if returned and supplied:
            if identifier(str(returned)) not in {identifier(value) for value in supplied}:
                return 'rejected', scheme + '_conflict'
            corroborated.append(scheme)
    return ('verified', '+'.join(corroborated)) if corroborated else ('ambiguous', 'no_shared_recording_identifier')


def select(rows):
    """Two recordings per membership/evidence stratum; never silently fill an empty cell."""
    result, used, counts = [], set(), {}
    ordered = sorted(rows, key=lambda r: hashlib.sha256((r['key'] + ':585-v1').encode()).hexdigest())
    for membership in ('library', 'discovery'):
        for evidence in ('conflicting', 'missing', 'retained'):
            pool = [r for r in ordered if r[membership] and r['evidence_stratum'] == evidence]
            counts[membership + ':' + evidence] = len(pool)
            for row in [r for r in pool if r['recording_id'] not in used][:2]:
                result.append({**row, 'stratum': membership + ':' + evidence})
                used.add(row['recording_id'])
    return result, counts


def freeze(conn, output):
    if (output / 'sample.json').exists():
        raise ValueError('Pinned sample already exists; use a new output directory')
    rows = [dict(row) for row in conn.execute('''SELECT c.id AS entry_id,c.recording_id,c.identity_key AS key,
        c.title,c.artist,c.album,c.mb_year,c.video_id,c.duration_s,c.genre,c.mb_recording_id,
        c.bpm,c.energy,c.danceability,c.instrumental,c.mood_acoustic,
        EXISTS(SELECT 1 FROM library_memberships l WHERE l.entry_id=c.id) AS library,
        EXISTS(SELECT 1 FROM discovery_memberships d WHERE d.recording_id=c.recording_id) AS discovery,
        EXISTS(SELECT 1 FROM metadata_conflicts m WHERE (m.recording_id=c.recording_id OR m.entry_id=c.id)
            AND m.status='unresolved') AS has_conflict
        FROM catalog_tracks c WHERE c.available IS NOT 0 AND c.video_id IS NOT NULL
        AND length(c.title)>=3 AND length(c.artist)>=3 ORDER BY c.id''')]
    # Collapse alternate playable items before sampling. Prefer duration-known items.
    unique = {}
    for row in sorted(rows, key=lambda r: (not bool(r['duration_s']), r['entry_id'])):
        row['evidence_stratum'] = ('conflicting' if row['has_conflict'] or
            (finite(row['bpm']) and finite(row['energy']) and
             ((row['bpm'] < 90 and row['energy'] > .7) or (row['bpm'] > 140 and row['energy'] < .35)))
            else 'missing' if row['bpm'] is None and row['energy'] is None else 'retained')
        unique.setdefault(row['recording_id'], row)
    sample, counts = select(list(unique.values()))
    for row in sample:
        row['identifiers'] = [dict(r) for r in conn.execute('''SELECT scheme,entity_type,value,source,status
            FROM recording_identifiers WHERE recording_id=? ORDER BY id''', (row['recording_id'],))]
        row['observations'] = [dict(r) for r in conn.execute('''SELECT o.id,o.field,o.number_value,o.unit,o.origin,
            o.legacy_source,d.provider FROM metadata_observations o LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id
            WHERE o.recording_id=? AND o.field IN ('bpm','energy','danceability','instrumental','mood_acoustic')
            ORDER BY o.id''', (row['recording_id'],))]
    save(output / 'sample.json', sample)
    plan = {'schema': 1, 'frozen_at': time.time(), 'sample_sha256': digest(output / 'sample.json'),
            'tool_sha256': digest(Path(__file__)), 'eligible_recordings': len(unique),
            'stratum_population': counts, 'selected': dict(Counter(r['stratum'] for r in sample)),
            'max_requests': 36, 'sample_kind': 'stratified_feasibility_not_population_estimate',
            'schema_version': conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]}
    save(output / 'plan.json', plan)
    print(json.dumps(plan, indent=2))


class CachedHTTP:
    def __init__(self, client, base, output, budget):
        self.client, self.base, self.output, self.budget = client, base, output, budget
        self.requests = json.loads((output / 'requests.json').read_text()) if (output / 'requests.json').exists() else []
        self.hits, self.last = 0, 0.

    def get(self, path, params=None):
        cache = self.output / 'cache' / (hashlib.sha256(json.dumps([self.base, path, params], sort_keys=True).encode()).hexdigest() + '.json')
        if cache.exists():
            self.hits += 1
            return json.loads(cache.read_text())
        if len(self.requests) >= self.budget:
            raise RuntimeError('request_budget_exhausted')
        time.sleep(max(0., 1.25 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        try:
            response = self.client.get(self.base + path, params=params)
        except httpx.HTTPError as error:
            self.requests.append({'path': path, 'error': type(error).__name__, 'wall_s': time.monotonic() - self.last})
            save(self.output / 'requests.json', self.requests)
            raise RuntimeError(type(error).__name__) from None
        self.requests.append({'path': path, 'status': response.status_code, 'bytes': len(response.content),
                              'wall_s': time.monotonic() - self.last, 'retry_after': response.headers.get('Retry-After')})
        save(self.output / 'requests.json', self.requests)
        if response.status_code == 429:
            raise RuntimeError('rate_limited_stop_without_retry')
        if response.status_code not in (200, 404):
            raise RuntimeError('HTTP_' + str(response.status_code))
        payload = response.json() if response.status_code == 200 else None
        save(cache, payload)
        return payload


def fetch(output):
    plan = json.loads((output / 'plan.json').read_text())
    if digest(output / 'sample.json') != plan['sample_sha256'] or digest(Path(__file__)) != plan['tool_sha256']:
        raise ValueError('Frozen sample or tool changed; create a new plan')
    sample = json.loads((output / 'sample.json').read_text())
    report = {'sample_sha256': plan['sample_sha256'], 'at': time.time(), 'items': [], 'state': 'complete',
              'whole_catalog_coverage_estimate': None, 'production_writes': 0}
    with httpx.Client(timeout=15, headers={'User-Agent': 'TuneConsole-metadata-pilot/2 (github.com/reubenfirmin/TuneConsole)'}) as client:
        api = CachedHTTP(client, 'https://api.reccobeats.com/v1', output / 'reccobeats', plan['max_requests'])
        try:
            for row in sample:
                query = {'searchText': row['title'], 'artist': row['artist'], 'size': 5}
                payload = api.get('/track/search', query) or {}
                assert api.get('/track/search', query) == payload
                candidates = []
                for candidate in payload.get('content', []):
                    state, reason = decide(row, candidate)
                    candidates.append({'candidate': candidate, 'decision': state, 'reason': reason})
                viable = [r for r in candidates if r['decision'] != 'rejected']
                verified = [r for r in viable if r['decision'] == 'verified']
                decision = 'verified' if len(verified) == 1 and len(viable) == 1 else 'ambiguous' if viable else 'no_match'
                item = {'key': row['key'], 'stratum': row['stratum'], 'decision': decision, 'candidates': candidates,
                        'features': None, 'comparisons': []}
                # Candidate-only values remain available for diagnosis, never useful-coverage counts.
                if len(viable) == 1:
                    item['features'] = api.get('/track/' + viable[0]['candidate']['id'] + '/audio-features')
                    for field, local in FIELDS.items():
                        value = (item['features'] or {}).get(field)
                        if finite(value):
                            observations = [o for o in row['observations'] if o['field'] == local and finite(o['number_value'])]
                            item['comparisons'].append({'field': field, 'provider_value': value, 'retained_value': row[local],
                                'incremental': decision == 'verified' and row[local] is None,
                                'retained_observations': [{**o, 'delta': value - o['number_value']} for o in observations]})
                report['items'].append(item)
                save(output / 'report.json', report)
        except (RuntimeError, ValueError, httpx.HTTPError) as error:
            report.update(state='incomplete', error=type(error).__name__ + ':' + str(error))
        report.update(requests=api.requests, cache_hits=api.hits,
                      response_bytes=sum(r.get('bytes', 0) for r in api.requests),
                      request_wall_s=sum(r['wall_s'] for r in api.requests))
    key = os.getenv('GETSONGBPM_API_KEY')
    attribution = os.getenv('TUNECONSOLE_GETSONGBPM_ATTRIBUTION')
    report['getsongbpm'] = {'key_configured': bool(key), 'attribution_configured': bool(attribution),
        'state': 'unavailable_prerequisites', 'requests': []}
    if key and attribution:
        with httpx.Client(timeout=15, headers={'X-API-KEY': key}) as client:
            api = CachedHTTP(client, 'https://api.getsong.co', output / 'getsongbpm', len(sample))
            result = report['getsongbpm']
            result.update(state='complete', items=[])
            try:
                for row in sample:
                    payload = api.get('/search/', {'type': 'both', 'lookup': f"song:{row['title']} artist:{row['artist']}", 'limit': 5})
                    result['items'].append({'key': row['key'], 'payload': payload,
                        'decision': 'unverified_no_recording_id_or_duration_contract'})
            except RuntimeError as error:
                result.update(state='incomplete', error=str(error))
            result.update(requests=api.requests, cache_hits=api.hits)
    report['summary'] = {'inputs': len(sample), 'decisions': dict(Counter(r['decision'] for r in report['items'])),
        'candidate_decisions': dict(Counter(c['reason'] for r in report['items'] for c in r['candidates'])),
        'incremental_verified_fields': sum(c['incremental'] for r in report['items'] for c in r['comparisons'])}
    report['cache_sha256'] = {str(p.relative_to(output)): digest(p) for p in output.glob('*/cache/*.json')}
    save(output / 'report.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('items', 'cache_sha256', 'requests')}, indent=2))


def connect(path):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'freeze', 'fetch'])
    parser.add_argument('--db', type=Path, default=Path.home() / '.local/share/yt-playlist/state.db')
    parser.add_argument('--output', type=Path, default=LOCAL)
    args = parser.parse_args()
    if args.action == 'fetch':
        fetch(args.output)
        return
    with closing(connect(args.db)) as conn:
        if args.action == 'freeze':
            freeze(conn, args.output)
            return
        tables = [row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        schema = {name: [row['name'] for row in conn.execute(f'PRAGMA table_info("{name}")')]
                  for name in tables if any(word in name for word in
                      ('catalog', 'recording', 'metadata', 'membership', 'observat', 'profile', 'identit', 'opportun', 'video'))}
        print(json.dumps({'schema': schema, 'getsongbpm_key_configured': bool(os.getenv('GETSONGBPM_API_KEY')),
                          'getsongbpm_attribution_configured': bool(os.getenv('TUNECONSOLE_GETSONGBPM_ATTRIBUTION'))}, indent=2))


if __name__ == '__main__':
    main()
