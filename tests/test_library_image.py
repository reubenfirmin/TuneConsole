"""Manager lifetime tests on tiny simulated storage; CI needs no Btrfs/Podman."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import pytest

import library_image as image


@pytest.fixture
def backend(tmp_path, monkeypatch):
    repository = tmp_path / 'repository'
    repository.mkdir()
    monkeypatch.setattr(image, 'ROOT', repository)
    volume = tmp_path / 'volume'
    volume.mkdir()
    flags, calls, containers = {}, [], set()
    failures = set()

    def command(*arguments, check=True, **options):
        arguments = tuple(str(arg) for arg in arguments)
        calls.append(arguments)
        result = SimpleNamespace(returncode=0, stdout='', stderr='')
        if arguments[:2] == ('stat', '-f'):
            result.stdout = 'btrfs\n'
        elif arguments[:3] == ('btrfs', 'subvolume', 'create'):
            path = Path(arguments[-1])
            path.mkdir()
            flags[path] = False
        elif arguments[:3] == ('btrfs', 'subvolume', 'snapshot'):
            source, path = map(Path, arguments[-2:])
            shutil.copytree(source, path)
            flags[path] = False
            if 'snapshot' in failures:
                raise RuntimeError('controlled partial snapshot failure')
        elif arguments[:3] == ('btrfs', 'property', 'set'):
            flags[Path(arguments[4])] = arguments[-1] == 'true'
        elif arguments[:3] == ('btrfs', 'property', 'get'):
            result.stdout = 'ro=' + str(flags[Path(arguments[4])]).lower()
        elif arguments[:3] == ('btrfs', 'subvolume', 'delete'):
            path = Path(arguments[-1])
            assert not flags[path], 'Frozen subvolume must be thawed only for deletion'
            shutil.rmtree(path)
            del flags[path]
        elif arguments[:3] == ('btrfs', 'filesystem', 'sync'):
            pass
        elif arguments[:2] == ('podman', 'run'):
            bindings = [arg for arg in arguments if ':/library:' in arg]
            target = Path(bindings[0].split(':/library:')[0])
            if arguments[-2] == 'export':
                if 'export' in failures:
                    raise RuntimeError('controlled capture failure')
                shutil.copyfile(arguments[-1], target / 'state.db')
                (target / '.capture.json').write_text(json.dumps(dict(schema=123, source_total_changes=0)))
        elif arguments[:3] == ('podman', 'image', 'inspect'):
            result.stdout = 'sha256:controlled-runtime'
        elif arguments[:2] == ('podman', 'rm'):
            containers.discard(arguments[-1])
        elif arguments[:3] == ('podman', 'container', 'exists'):
            result.returncode = 0 if arguments[-1] in containers else 1
        else:
            raise AssertionError(arguments)
        return result

    monkeypatch.setattr(image, 'command', command)
    source = tmp_path / 'native.db'
    source.write_bytes(b'controlled donor bytes')
    manager = image.Manager(volume).initialize()
    return SimpleNamespace(manager=manager, source=source, calls=calls, flags=flags,
                           failures=failures, containers=containers, root=tmp_path)


def test_concurrent_preparation_captures_once_and_reuses_frozen_identity(backend):
    with ThreadPoolExecutor(2) as workers:
        values = list(workers.map(lambda _: backend.manager.prepare(backend.source), range(2)))
    assert values[0]['id'] == values[1]['id']
    exports = [call for call in backend.calls if call[:2] == ('podman', 'run') and call[-2] == 'export']
    assert len(exports) == 1
    assert backend.flags[backend.manager.volume / 'baselines' / values[0]['id']] is True
    assert not list((backend.manager.volume / 'runs').iterdir())
    assert backend.source.read_bytes() == b'controlled donor bytes'


def test_acquisition_does_not_repeat_capture_or_tree_verification(backend, monkeypatch):
    metadata = backend.manager.prepare(backend.source)
    from tests import library_image_capture
    monkeypatch.setattr(library_image_capture, 'verify_tree', lambda *_: pytest.fail('repeated full scan'))
    backend.calls.clear()
    with backend.manager.workspace(baseline=metadata['id'], report=backend.root / 'report') as run:
        (run['path'] / 'state.db').write_bytes(b'private mutation')
        assert (backend.manager.volume / 'baselines' / metadata['id'] / 'state.db').read_bytes() == b'controlled donor bytes'
    assert not [call for call in backend.calls if call[:2] == ('podman', 'run')]
    assert not list((backend.manager.volume / 'workspaces').iterdir())
    assert (backend.root / 'report/workspace.json').is_file()


def test_failed_capture_preserves_previous_published_generation(backend):
    original = backend.manager.prepare(backend.source)
    backend.failures.add('export')
    with pytest.raises(RuntimeError, match='capture failure'):
        backend.manager.prepare(backend.source, refresh=True)
    assert backend.manager.resolve(source=backend.source)['id'] == original['id']
    assert list((backend.manager.volume / 'baselines').iterdir()) == [backend.manager.volume / 'baselines' / original['id']]
    assert not list((backend.manager.volume / 'runs').iterdir())


def test_failed_partial_acquisition_removes_workspace_and_receipt(backend):
    metadata = backend.manager.prepare(backend.source)
    backend.failures.add('snapshot')
    with pytest.raises(RuntimeError, match='partial snapshot'):
        with backend.manager.workspace(baseline=metadata['id'], report=backend.root / 'report'):
            pytest.fail('yielded a failed snapshot')
    assert not list((backend.manager.volume / 'runs').iterdir())
    assert not list((backend.manager.volume / 'workspaces').iterdir())


def test_refresh_keeps_active_generation_pinned_and_gc_preserves_latest(backend):
    first = backend.manager.prepare(backend.source)
    with backend.manager.workspace(baseline=first['id'], report=backend.root / 'active') as active:
        second = backend.manager.prepare(backend.source, refresh=True)
        latest = backend.manager.prepare(backend.source, refresh=True)
        candidates = backend.manager.collect(keep=1, delete=False)
        second_path = str(backend.manager.volume / 'baselines' / second['id'])
        assert candidates == [second_path]
        assert backend.manager.collect(keep=1, delete=True) == candidates
        assert (active['path'] / 'state.db').exists()
        assert backend.manager.resolve(baseline=first['id'])['id'] == first['id']
    assert backend.manager.collect(keep=1, delete=True) == [str(backend.manager.volume / 'baselines' / first['id'])]
    assert backend.manager.resolve(source=backend.source)['id'] == latest['id']


def test_gc_previews_orphan_container_and_never_stops_live_owner(backend):
    baseline = backend.manager.prepare(backend.source)
    with backend.manager.workspace(baseline=baseline['id'], report=backend.root / 'live') as active:
        backend.containers.add(active['name'])
        assert backend.manager.collect(keep=1, delete=True) == []
        assert active['name'] in backend.containers
    identity = uuid.uuid4().hex
    target = backend.manager.volume / 'workspaces' / identity
    target.mkdir()
    backend.flags[target] = False
    owner = dict(image.process_identity(), pid=999999999)
    name = 'tuneconsole-library-run-' + identity
    backend.containers.add(name)
    image.save(backend.manager.volume / 'runs' / (identity + '.json'),
               dict(id=identity, baseline=baseline['id'], container=name, kind='workspace', owner=owner))
    assert backend.manager.collect(delete=False) == ['container:' + name]
    assert name in backend.containers and target.is_dir()
    assert backend.manager.collect(delete=True) == ['container:' + name, str(target)]
    assert name not in backend.containers and not target.exists()


def test_runtime_mounts_hide_other_workspaces_and_only_preparation_gets_donor(backend):
    normal = backend.manager.container_args(backend.root / 'run', backend.root / 'report', 'name')
    assert str(backend.source.parent) + ':' + str(backend.source.parent) + ':ro' not in normal
    assert '/worktree/build:rw,size=256m,mode=1777,nodev,nosuid,notmpcopyup' in normal
    capture = backend.manager.container_args(backend.root / 'run', backend.root / 'report', 'name', donor=backend.source)
    assert str(backend.source.parent) + ':' + str(backend.source.parent) + ':ro' in capture


def test_unmanaged_adapters_keep_ci_copy_behavior_without_image_tools(monkeypatch):
    from tests.library_image_fixtures import managed_database
    monkeypatch.delenv('TUNECONSOLE_IMAGE_RUN', raising=False)
    monkeypatch.setenv('TUNECONSOLE_TEST_LIBRARY', '/does/not/exist')
    assert managed_database() is None


def test_generation_identity_rejects_path_escape(backend):
    with pytest.raises(ValueError, match='identity'):
        backend.manager.metadata('../native.db')
