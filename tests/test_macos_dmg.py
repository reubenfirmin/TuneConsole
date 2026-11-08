"""Exercise image-creation recovery without requiring a macOS build host."""
import json
import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

import pytest


CREATE_DMG = Path(__file__).parents[1] / 'packaging/macos/create-dmg.sh'
BUSY = 'hdiutil: create failed - Resource busy'


@pytest.mark.parametrize(('results', 'status', 'waits'), [
    ([(0, 'created')], 0, []),
    ([(1, BUSY), (0, 'created')], 0, ['1']),
    ([(1, BUSY), (16, BUSY), (0, 'created')], 0, ['1', '2']),
    ([(1, BUSY), (16, BUSY), (23, BUSY)], 23, ['1', '2']),
    ([(7, 'hdiutil: create failed - Permission denied')], 7, []),
    ([(1, BUSY), (5, 'hdiutil: create failed - No space left on device')], 5, ['1']),
])
def test_dmg_creation_retries_only_busy_and_preserves_failure(tmp_path, results, status, waits):
    tools = tmp_path / 'tools'
    tools.mkdir()
    hdiutil = tools / 'hdiutil'
    hdiutil.write_text(f'#!{sys.executable}\n' + dedent('''\
        import json, os, sys
        from pathlib import Path
        log = Path(os.environ['DMG_CALLS'])
        calls = json.loads(log.read_text()) if log.exists() else []
        status, message = json.loads(os.environ['DMG_RESULTS'])[len(calls)]
        calls.append(sys.argv[1:])
        log.write_text(json.dumps(calls))
        print(message)
        if status == 0:
            Path(sys.argv[-1]).write_bytes(b'controlled image')
        sys.exit(status)
    '''))
    sleep = tools / 'sleep'
    sleep.write_text(f'#!{sys.executable}\n' + dedent('''\
        import os, sys
        with open(os.environ['DMG_WAITS'], 'a') as log:
            log.write(sys.argv[1] + '\\n')
    '''))
    for tool in (hdiutil, sleep):
        tool.chmod(0o755)
    source = tmp_path / 'signed app staging'
    source.mkdir()
    signed_app = source / 'TuneConsole.app'
    signed_app.write_bytes(b'signed app remains intact')
    (source / 'Applications').symlink_to('/Applications')
    destination = tmp_path / 'TuneConsole version.dmg'
    calls_log, waits_log = tmp_path / 'calls.json', tmp_path / 'waits.txt'

    completed = subprocess.run(
        ['bash', str(CREATE_DMG), str(source), str(destination)],
        env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
             'DMG_RESULTS': json.dumps(results), 'DMG_CALLS': str(calls_log),
             'DMG_WAITS': str(waits_log)},
        capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == status
    assert json.loads(calls_log.read_text()) == [
        ['create', '-volname', 'TuneConsole', '-srcfolder', str(source),
         '-ov', '-format', 'UDZO', str(destination)]
    ] * len(results)
    assert (waits_log.read_text().splitlines() if waits_log.exists() else []) == waits
    for _status, message in results:
        assert message in completed.stdout + completed.stderr
    assert destination.exists() == (status == 0)
    assert signed_app.read_bytes() == b'signed app remains intact'
    assert (source / 'Applications').readlink() == Path('/Applications')
