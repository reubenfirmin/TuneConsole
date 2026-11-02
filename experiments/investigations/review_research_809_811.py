"""Independent frozen execution of completed representation research acceptance checks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'experiments/audio_lab/local/review-research-809-811-20261101'
BASES = ('src', 'tests', 'experiments/audio_lab_tools', 'experiments/investigations')
FILES = ('conftest.py', 'pyproject.toml', 'ci_test_shards.py',
         'local_test_workers.py', 'experiments/run.py')


def manifest(root):
    paths = [p for name in BASES for p in (root/name).rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    paths.extend(root/name for name in FILES)
    paths.extend((root/'experiments/audio_lab').glob('*.md'))
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths}


def test_completed_research_on_frozen_source():
    REPORT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='runtime-', dir=REPORT) as temporary:
        frozen = Path(temporary)
        before = manifest(ROOT)
        for name in BASES:
            (frozen/name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(ROOT/name, frozen/name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in FILES:
            (frozen/name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT/name, frozen/name)
        (frozen/'experiments/audio_lab').mkdir(parents=True)
        for path in (ROOT/'experiments/audio_lab').glob('*.md'):
            shutil.copyfile(path, frozen/'experiments/audio_lab'/path.name)
        assert manifest(frozen) == before
        (REPORT/'manifest.json').write_text(json.dumps({
            'sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
            'files': before}, indent=2))
        # Keep study directories inside the frozen experiments boundary. Other
        # retained evidence is read through links; archived absolute references
        # still identify the original receipts, not invented relocated inputs.
        local = frozen/'experiments/audio_lab/local'
        local.mkdir()
        studies = {'mert-layers-809-20261101', 'chroma-810-20261101', 'midlevel-811-20261101'}
        for path in (ROOT/'experiments/audio_lab/local').iterdir():
            if path.name in studies:
                shutil.copytree(path, local/path.name)
            else:
                (local/path.name).symlink_to(path, target_is_directory=path.is_dir())
        (frozen/'.venv').symlink_to(ROOT/'.venv', target_is_directory=True)
        (frozen/'extension').mkdir()
        (frozen/'extension/audio_model').symlink_to(ROOT/'extension/audio_model', target_is_directory=True)
        (frozen/'process-tmp').mkdir()
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(frozen/'src'), str(frozen))),
               'TMPDIR': str(frozen/'process-tmp'), 'PYTHONDONTWRITEBYTECODE': '1',
               'UV_NO_SYNC': '1'}
        commands = [
            ['representation-study', 'verify'],
            ['chroma-study', 'verify'],
            ['midlevel-study', 'verify'],
            ['midlevel-pilot', 'verify'],
            ['investigate', 'representation_809.py',
             str(frozen/'experiments/audio_lab_tools/test_representation_study.py')],
            ['investigate', 'mert_checkpoint_809.py'],
            ['investigate', 'chroma_810.py'],
            ['investigate', 'midlevel_811.py'],
            ['investigate', 'midlevel_replay_811.py'],
            ['investigate', 'review_research_results_809_811.py'],
        ]
        results = []
        try:
            for index, arguments in enumerate(commands):
                command = [str(frozen/'experiments/run.py'), *arguments]
                if arguments[0] == 'investigate':
                    command.extend(['--basetemp='+str(frozen/f'pytest-{index}'),
                                    '--junitxml='+str(REPORT/f'checks-{index}.xml'), '--tb=short'])
                (REPORT/f'command-{index}.json').write_text(json.dumps(command, indent=2))
                with (REPORT/f'output-{index}.txt').open('w') as output:
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
                (REPORT/'commands.json').write_text(json.dumps(results, indent=2))
                print(json.dumps(results[-1]), flush=True)
            assert all(row['returncode'] == 0 for row in results), results
        finally:
            current = manifest(ROOT)
            (REPORT/'continuity.json').write_text(json.dumps({
                'runtime_unchanged': manifest(frozen) == before,
                'checkout_changed': [key for key in before if current.get(key) != before[key]]}, indent=2))
