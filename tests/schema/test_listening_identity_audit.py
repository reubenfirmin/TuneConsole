"""Read-only comparison before the listening identity cutover."""
import json
from pathlib import Path
import sqlite3

import pytest


def test_actual_listening_identity_and_canonical_credit_output_audit(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained isolated actual-library donor')
    source = Path(option).resolve(strict=True)
    assert source.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        version = conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert 95<=version<=99
        counts = {table:conn.execute('SELECT count(*) FROM '+table).fetchone()[0]
                  for table in ('play_events','listening_observations','player_events','play_event_sources')}
        unresolved = {table:conn.execute('SELECT count(*) FROM '+table+' WHERE recording_id IS NULL').fetchone()[0]
                      for table in ('play_events','listening_observations')}
        mismatches = {table:conn.execute('SELECT count(*) FROM '+table+' e JOIN recordings r ON r.id=e.recording_id '
                     'WHERE e.identity_key<>r.identity_key').fetchone()[0]
                      for table in ('play_events','listening_observations')}
        assert not any(mismatches.values())
        old_tracks = {r[0]:r[1] for r in conn.execute('SELECT identity_key,min(day) FROM listening_activity GROUP BY identity_key')}
        old_artists = {r[0]:r[1] for r in conn.execute('SELECT t.artist,min(a.day) FROM listening_activity a '
            'JOIN tracks t ON t.identity_key=a.identity_key WHERE t.artist<>\'\' GROUP BY t.artist')}
        canonical = [dict(r) for r in conn.execute('WITH credits AS MATERIALIZED ('
            'SELECT DISTINCT e.recording_id,coalesce(d.artist_id,c.artist_id) artist_id FROM catalog_entries e '
            'JOIN entry_artists c ON c.entry_id=e.id LEFT JOIN artist_redirects d ON d.retired_id=c.artist_id) '
            'SELECT a.identity_id,c.artist_id,n.name,min(a.day) first_day FROM listening_activity a '
            'JOIN credits c ON c.recording_id=a.recording_id JOIN artists n ON n.id=c.artist_id '
            'GROUP BY a.identity_id,c.artist_id ORDER BY a.identity_id,c.artist_id')]
        by_name = {}
        for row in canonical:
            by_name[row['name']] = min(by_name.get(row['name'],row['first_day']),row['first_day'])
        changes = []
        for name in sorted(set(old_artists)|set(by_name)):
            old,new = old_artists.get(name),by_name.get(name)
            if old!=new:
                changes.append({'name':name,'old_first_day':old,'canonical_first_day':new,
                    'reason':'canonical_credit_added' if old is None else 'display_credit_removed' if new is None else 'canonical_credit_earlier_or_later'})
        report = {'schema':version,'counts':counts,'unresolved':unresolved,'mismatched_resolved_keys':mismatches,
            'raw_current_distinctions':conn.execute('SELECT count(*) FROM listening_observations WHERE raw_identity_key<>identity_key').fetchone()[0],
            'raw_uploads_outside_catalog':conn.execute('SELECT count(*) FROM player_events p WHERE p.video_id IS NOT NULL '
                'AND NOT EXISTS(SELECT 1 FROM youtube_items y WHERE y.video_id=p.video_id)').fetchone()[0],
            'old_recording_or_unresolved_first_days':len(old_tracks),'old_display_artist_first_days':len(old_artists),
            'canonical_account_artist_first_days':len(canonical),'canonical_names':len(by_name),
            'output_changes':changes,'sqlite':sqlite3.sqlite_version}
        assert counts['play_events']>=3444 and counts['listening_observations']>=3845
        (tmp_path/'listening-identity-before.json').write_text(json.dumps(report,indent=2))
        print({key:value for key,value in report.items() if key!='output_changes'})
        print({'classified_artist_changes':len(changes),'examples':changes[:12]})
