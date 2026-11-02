"""Independently execute completed WCMED/SiTunes checks on frozen research sources."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from experiments.investigations.review_research_809_811 import BASES, FILES, ROOT, manifest

REPORT = ROOT / 'experiments/audio_lab/local/review-research-812-813-20261101'


def test_completed_external_studies_on_frozen_source():
    REPORT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='runtime-', dir=REPORT) as temporary:
        frozen = Path(temporary)
        before = manifest(ROOT)
        for name in BASES:
            (frozen / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(ROOT / name, frozen / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in FILES:
            (frozen / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, frozen / name)
        lab = frozen / 'experiments/audio_lab'
        lab.mkdir(parents=True)
        for path in (ROOT / 'experiments/audio_lab').glob('*.md'):
            shutil.copyfile(path, lab / path.name)
        assert manifest(frozen) == before
        (REPORT / 'manifest.json').write_text(json.dumps({
            'sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
            'files': before}, indent=2))
        local = lab / 'local'
        local.mkdir()
        final_rounds = {'wcmed-812-20261101': 'round-3', 'situnes-813-20261101': 'round-2'}
        for path in (ROOT / 'experiments/audio_lab/local').iterdir():
            if path.name in final_rounds:
                selected = final_rounds[path.name]
                (local / path.name).mkdir()
                shutil.copytree(path / selected, local / path.name / selected)
            else:
                (local / path.name).symlink_to(path, target_is_directory=path.is_dir())
        (frozen / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
        (frozen / 'extension').mkdir()
        (frozen / 'extension/audio_model').symlink_to(ROOT / 'extension/audio_model', target_is_directory=True)
        (frozen / 'process-tmp').mkdir()
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(frozen / 'src'), str(frozen))),
               'TMPDIR': str(frozen / 'process-tmp'), 'PYTHONDONTWRITEBYTECODE': '1', 'UV_NO_SYNC': '1'}
        commands = [['wcmed-study', 'verify'], ['situnes-study', 'verify'],
                    ['investigate', 'wcmed_812.py'], ['investigate', 'situnes_813.py']]
        results = []
        try:
            for index, arguments in enumerate(commands):
                command = [str(frozen / 'experiments/run.py'), *arguments]
                if arguments[0] == 'investigate':
                    command.extend(['--basetemp=' + str(frozen / f'pytest-{index}'),
                                    '--junitxml=' + str(REPORT / f'checks-{index}.xml'), '--tb=short'])
                (REPORT / f'command-{index}.json').write_text(json.dumps(command, indent=2))
                with (REPORT / f'output-{index}.txt').open('w') as output:
                    with subprocess.Popen(command, cwd=frozen, env=env, stdout=output,
                                          stderr=subprocess.STDOUT, start_new_session=True) as child:
                        try:
                            code = child.wait(timeout=300)
                        finally:
                            try:
                                os.killpg(child.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            try:
                                child.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                results.append({'arguments': arguments, 'returncode': code})
                (REPORT / 'commands.json').write_text(json.dumps(results, indent=2))
                print(json.dumps(results[-1]), flush=True)
            assert all(row['returncode'] == 0 for row in results), results
        finally:
            current = manifest(ROOT)
            (REPORT / 'continuity.json').write_text(json.dumps({
                'runtime_unchanged': manifest(frozen) == before,
                'checkout_changed': [key for key in before if current.get(key) != before[key]]}, indent=2))
