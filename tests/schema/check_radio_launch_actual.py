"""Opt-in current-code Radio launch replay on a disposable actual-library copy."""
import hashlib
import json
from pathlib import Path
import sys

import pytest
import uvloop

from tests.core.check_startup_background_actual import check
from tests.schema.actual_library import copy_readonly_library
from tests.library_image.fixtures import report_file


def test_actual_library_radio_launches(request, tmp_path, monkeypatch):
    option = request.config.getoption('--schema-source-db')
    if not option:
        pytest.skip('Supply a read-only source for an isolated actual-library replay')
    path = Path(option).resolve(strict=True)
    destination = tmp_path / 'tuneconsole-radio.db'
    report_path = report_file('/tmp/tuneconsole-633-actual-copy.json')
    report = {'source': str(path), 'source_access': 'SQLite mode=ro; files read only',
              'python': sys.version, 'status': 'copying', 'source_hashes': {}}
    for name in ('web/routes/radio_session.py', 'web/routes/home_access.py', 'rec/account_growth.py',
                 'web/radio_session.py', 'web/static/radio.js'):
        source = Path(__file__).resolve().parents[2] / 'src/yt_playlist' / name
        report['source_hashes'][name] = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        destination = copy_readonly_library(path, destination, monkeypatch, report)
        report['status'] = 'running'
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        # Radio's synchronous observer runs a second loop on its worker. Match
        # the Linux application's loop in both places for bridge response wakeup.
        import asyncio
        policy = asyncio.get_event_loop_policy()
        try:
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            uvloop.run(check(destination, report_file('/tmp/tuneconsole-633-actual-launch.json'),
                             launch_radio=True, check_ownership=True))
        finally:
            asyncio.set_event_loop_policy(policy)
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed', error=repr(exc))
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + '\n')
