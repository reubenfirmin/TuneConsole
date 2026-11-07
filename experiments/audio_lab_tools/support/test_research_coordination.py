"""Research dispatch stays independent while experiment writer locks still work."""
import ast
import fcntl
import importlib
import importlib.util
import io
from pathlib import Path
import sys

import pytest

from .. import TOOLS, module_name, source_path


def test_stable_commands_dispatch_into_grouped_packages(tmp_path, monkeypatch):
    from experiments import run

    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run, 'ROOT', tmp_path)
    calls = []
    monkeypatch.setattr(run.os, 'execv', lambda executable, command: calls.append(command))
    for command, name in run.MODULES.items():
        run.main([command])
        target = calls[-1][3]
        assert target == module_name(name)
        assert importlib.util.find_spec(target).origin == str(source_path(name + '.py'))
        assert len(target.split('.')) == 4


def test_named_research_checks_resolve_colocated_tests(monkeypatch):
    from experiments import run

    monkeypatch.chdir(run.ROOT)
    monkeypatch.setattr(run.shutil, 'which', lambda name: '/fake/uv')
    calls = []
    monkeypatch.setattr(run.os, 'execv', lambda executable, command: calls.append(command))
    run.main(['check', '--test', 'test_dataset_windows.py', '--test', 'test_library_replay.py'])
    assert calls[0][-2:] == [str(TOOLS / 'datasets/test_dataset_windows.py'),
                             str(TOOLS / 'replay/test_library_replay.py')]


def test_source_lookup_rejects_missing_ambiguous_and_nonlocal_names():
    for filename in ('missing-research-tool.py', '__init__.py', '../run.py'):
        with pytest.raises(ValueError):
            source_path(filename)


@pytest.mark.parametrize('name,function', [
    ('mapped_vibe_round', 'sources'), ('vibe_specialist_round', 'sources'),
    ('suitability_experiment', 'code_paths'), ('cruise_baselines', 'code_paths'),
    ('heldout_cohort', 'inference_hashes'),
])
def test_source_manifests_include_package_initializers(name, function):
    manifest = getattr(load(name), function)()
    assert any(path.endswith('audio_lab_tools/__init__.py') for path in manifest)
    assert any(path.endswith('audio_lab_tools/training/__init__.py') for path in manifest)


@pytest.fixture(autouse=True)
def forbid_suite_lock_access(monkeypatch):
    original = io.open

    def guarded(path, *args, **kwargs):
        if isinstance(path, (str, bytes, Path)) and 'tuneconsole-tests-' in str(path):
            pytest.fail('Research tried to open the global-suite lock: ' + str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(io, 'open', guarded)
    runtime = importlib.import_module('scripts.audio_model.build_runtime')
    monkeypatch.setattr(runtime, 'machine_lock',
                        lambda *a, **kw: pytest.fail('Research called the global-suite lock helper'),
                        raising=False)


def load(name):
    return importlib.import_module(module_name(name))


def test_research_modules_do_not_import_global_suite_coordination():
    for path in TOOLS.rglob('*.py'):
        if path.name.startswith('test_'):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'scripts.audio_model.build_runtime':
                assert all(alias.name != 'machine_lock' for alias in node.names), path


@pytest.mark.parametrize('name,arguments,callback', [
    ('cruise_production_replay', ['--directory', '{directory}'], 'replay'),
    ('cruise_export', ['verify', '--directory', '{directory}'], 'verify'),
    ('cruise_package_check', ['--directory', '{directory}'], 'check'),
    ('focus_experiment', ['fit', '--directory', '{directory}'], 'fit'),
    ('reference_pipeline', [], 'evaluate'),
    ('browser_capture', [], 'run'),
    ('browser_preprocess', ['--report', '{directory}/report.json'], 'run'),
    ('dataset_windows', ['extract'], 'extract'),
    ('audio_evaluation', ['compare', '--directory', '{directory}'], 'compare'),
    ('preprocessing_quality', ['extract', '--directory', '{directory}'], 'extract'),
])
def test_default_and_read_only_dispatch_reaches_work_without_suite_lock(
        tmp_path, monkeypatch, name, arguments, callback):
    module = load(name)
    directory = tmp_path / 'local/round'
    monkeypatch.setattr(module, 'LAB', tmp_path, raising=False)
    if hasattr(module, 'inside'):
        monkeypatch.setattr(module, 'inside', lambda path: Path(path))
    calls = []
    monkeypatch.setattr(module, callback, lambda *args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(sys, 'argv', [name, *[arg.format(directory=directory) for arg in arguments]])
    module.main()
    assert len(calls) == 1
    args, _kwargs = calls[0]
    if name in ('browser_capture', 'dataset_windows'):
        assert args == (None,)
    elif name in ('audio_evaluation', 'preprocessing_quality'):
        assert args[-1] is None


@pytest.mark.parametrize('action', ['fresh', 'paired-slice', 'fresh-consumers', 'finish-constraints'])
def test_library_defaults_reach_work_and_record_independent_execution(tmp_path, monkeypatch, action):
    module = load('library_replay')
    monkeypatch.setattr(module, 'WORK', tmp_path)
    calls = []
    work = lambda *args, **kwargs: calls.append((args, kwargs))
    arguments = [action]
    if action == 'fresh':
        monkeypatch.setattr(module, 'fresh', work)
    elif action == 'paired-slice':
        monkeypatch.setattr(load('paired_campaign'), 'run', work)
    else:
        monkeypatch.setattr(load('replay_followups'), action.replace('-', '_'), work)
        if action == 'finish-constraints':
            arguments += ['--resume', str(tmp_path)]
    monkeypatch.setattr(sys, 'argv', ['library', *arguments])
    module.main()
    assert len(calls) == 1
    if action != 'fresh':
        assert calls[0][1]['independent_execution'] is True


@pytest.mark.parametrize('name', ['candidate_followup', 'mapped_vibe_round', 'transfer_component_repair'])
def test_round_defaults_keep_their_own_writer_exclusion(tmp_path, monkeypatch, name):
    module = load(name)
    directory = tmp_path / 'round'
    directory.mkdir()
    monkeypatch.setattr(module, 'inside', lambda path: Path(path))
    validations, stages = [], []

    def validate(*args, **kwargs):
        validations.append(kwargs)
        return {'transfer': [('left', 'right')]}

    monkeypatch.setattr(module, 'validate', validate)
    monkeypatch.setattr(module, 'stage', lambda _dir, name, _callback: stages.append(name))
    if name == 'mapped_vibe_round':
        monkeypatch.setattr(module, 'final_report', lambda _dir: None)
    monkeypatch.setattr(sys, 'argv', [name, 'run', '--directory', str(directory)])
    lock_path = directory.parent / (directory.name + '.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match='already running'):
            module.main()
        assert not validations and not stages
    module.main()
    assert validations and stages
    if name != 'mapped_vibe_round':
        assert validations[0] == {'execution': True}


def test_native_subprocess_keeps_limits_without_a_lifetime_descriptor(tmp_path, monkeypatch):
    module = load('audio_evaluation')
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(module, 'load', lambda path: {'output': str(path)})
    result = module.native(tmp_path / 'input.f32', tmp_path, None)
    assert result['output'].endswith('input.native.json')
    _args, kwargs = calls[0]
    assert kwargs['pass_fds'] == ()
    assert kwargs['timeout'] == 120 and kwargs['check']
    assert {key: kwargs['env'][key] for key in (
        'TF_NUM_INTRAOP_THREADS', 'TF_NUM_INTEROP_THREADS', 'OMP_NUM_THREADS')} == {
        'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1', 'OMP_NUM_THREADS': '1'}


@pytest.fixture
def runtime_builder(tmp_path, monkeypatch):
    import hashlib

    module = importlib.import_module('scripts.audio_model.build_runtime')
    source, sdk, eigen = (tmp_path / name for name in ('source', 'sdk', 'eigen'))
    for path in (source, sdk, eigen):
        path.mkdir()
    root = tmp_path / 'audio-model'
    (root / 'vendor').mkdir(parents=True)
    (root / 'vendor/bundle.json').write_text('{}')
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module, 'LOCK_PATH', tmp_path / 'audio-build.lock')
    monkeypatch.setattr(module, 'LOCK_WAIT_SECONDS', 0)
    # A controlled empty header tree reaches dispatch without installing a toolchain.
    monkeypatch.setattr(module, 'EIGEN_HEADERS', hashlib.sha256(b'').hexdigest())
    replies = iter([module.ESSENTIA, '', module.EMSDK, '', 'emcc 3.1.64'])
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **kw: next(replies))
    monkeypatch.setattr(sys, 'argv', ['builder', '--source', str(source),
                                    '--emsdk', str(sdk), '--eigen', str(eigen)])
    return module


def test_runtime_builder_dispatch_uses_only_its_own_lifetime_lock(runtime_builder, monkeypatch):
    import json
    import os

    module = runtime_builder
    calls = []

    def compile(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs['check'] and kwargs['env']['EMCC_CORES'] == '2'
        descriptor, = kwargs['pass_fds']
        assert Path(os.readlink(f'/proc/self/fd/{descriptor}')) == module.LOCK_PATH.with_name(
            'audio-build-workers.lock')
        for path in (module.LOCK_PATH, module.LOCK_PATH.with_name('audio-build-workers.lock')):
            with path.open('a+') as competing:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if '-o' in command:
            Path(command[command.index('-o') + 1]).write_bytes(b'controlled compiled runtime')

    monkeypatch.setattr(module.subprocess, 'run', compile)
    module.main()
    assert len(calls) == 3
    assert calls[1][0][-2:] == ['build', '-j2']
    manifest = json.loads((module.ROOT / 'vendor/runtime.json').read_text())
    assert manifest['essentia_commit'] == module.ESSENTIA
    assert manifest['emsdk_commit'] == module.EMSDK
    assert manifest['files'][0]['bytes'] == len(b'controlled compiled runtime')
    # Both barriers release after a successful build.
    with module.build_lock():
        pass


@pytest.mark.parametrize('held', ['audio-build.lock', 'audio-build-workers.lock'])
def test_runtime_builder_rejects_existing_coordinator_or_compiler(
        runtime_builder, monkeypatch, held):
    module = runtime_builder
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('Concurrent compiler started'))
    with module.LOCK_PATH.with_name(held).open('a+') as existing:
        existing.write('existing build owner')
        existing.flush()
        fcntl.flock(existing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match='Audio build lock remains busy'):
            module.main()
        existing.seek(0)
        assert existing.read() == 'existing build owner'
