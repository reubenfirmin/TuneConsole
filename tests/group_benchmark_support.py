"""Read-only frozen inputs shared by group experiments and budget acceptance."""
import json
import os
from pathlib import Path
import sqlite3

from yt_playlist.core import prepared_inputs


def frozen_inputs(request, tmp_path):
    snapshot = request.config.getoption('group_snapshot') or os.getenv('TUNECONSOLE_GROUP_SNAPSHOT')
    database = request.config.getoption('group_readonly_db') or os.getenv('TUNECONSOLE_GROUP_READONLY_DB')
    if database:
        with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='prepared_recording_inputs'").fetchone():
                rows = conn.execute('SELECT r.identity_key,p.*,m.payload profile FROM prepared_recording_inputs p '
                    'JOIN recordings r ON r.id=p.recording_id LEFT JOIN rec_musical_profiles m '
                    'ON m.identity_key=r.identity_key ORDER BY r.identity_key').fetchall()
                frozen = {'rows': [[r['identity_key'], prepared_inputs.payload(r),
                    json.loads(r['profile']) if r['profile'] else None] for r in rows]}
            else:
                rows = conn.execute('SELECT p.identity_key,p.payload,m.payload FROM rec_prepared_content p '
                                    'LEFT JOIN rec_musical_profiles m USING(identity_key) ORDER BY p.identity_key').fetchall()
                frozen = {'rows': [[key, json.loads(raw), json.loads(profile) if profile else None]
                                   for key, raw, profile in rows]}
        snapshot = snapshot or str(tmp_path / 'musical-group-inputs.json')
        Path(snapshot).write_text(json.dumps(frozen))
    return json.loads(Path(snapshot).read_text()) if snapshot else None


def report_path(request, tmp_path, default):
    return Path(request.config.getoption('group_report') or os.getenv('TUNECONSOLE_GROUP_REPORT')
                or tmp_path / default)
