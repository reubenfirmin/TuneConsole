"""Exercise the source archive through a real disposable python -m process."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.audio_lab_tools.support import research_artifacts
from experiments.audio_lab_tools.support.quality_round import EXPERIMENTS, seal, validate, verify


@pytest.fixture
def completed_round(tmp_path):
    (tmp_path / 'plan.json').write_text(json.dumps({'inputs': {}, 'code': {}, 'references': {}}))
    for stage in ('extraction', 'evaluation', 'postrun-source'):
        output = tmp_path / (stage + '.json')
        output.write_text(json.dumps({'stage': stage}))
        seal(tmp_path, stage, [output])
    return tmp_path


def verify_completed(directory):
    return verify(directory, required_stages={'extraction', 'evaluation'},
                  optional_stages={'postrun-source'})


def test_original_stages_allow_only_named_verified_provenance_addenda(completed_round):
    assert verify_completed(completed_round)['receipts'] == 3
    (completed_round / 'postrun-source.receipt.json').unlink()
    assert verify_completed(completed_round)['receipts'] == 2


def test_missing_original_stage_cannot_be_replaced_by_an_addendum(completed_round):
    (completed_round / 'extraction.receipt.json').unlink()
    with pytest.raises(ValueError, match="missing=.*extraction"):
        verify_completed(completed_round)


def test_unrecognized_receipt_cannot_replace_an_original_stage(completed_round):
    (completed_round / 'extraction.receipt.json').rename(completed_round / 'other.receipt.json')
    with pytest.raises(ValueError, match="unexpected=.*other"):
        verify_completed(completed_round)


@pytest.mark.parametrize('stage', ['extraction', 'postrun-source'])
@pytest.mark.parametrize('corrupt', ['receipt', 'output'])
def test_corrupt_original_or_admitted_provenance_fails(completed_round, stage, corrupt):
    if corrupt == 'receipt':
        path = completed_round / (stage + '.receipt.json')
        receipt = json.loads(path.read_text())
        receipt['plan_sha256'] = 'wrong plan'
        path.write_text(json.dumps(receipt))
    else:
        (completed_round / (stage + '.json')).write_text('changed output')
    with pytest.raises(ValueError, match='Receipt (plan identity differs|output changed)'):
        verify_completed(completed_round)


@pytest.mark.parametrize('location', ['source', 'local'])
def test_removed_protocol_references_keep_their_original_hash_checks(tmp_path, monkeypatch, location):
    original = tmp_path / 'experiments/audio_lab/OLD_PLAN.md'
    archive = tmp_path / 'protocols'
    monkeypatch.setattr(research_artifacts, 'LEGACY_PROTOCOLS', archive)
    path = tmp_path / 'source/audio_lab/OLD_PLAN.md' if location == 'source' else archive / original.name
    path.parent.mkdir(parents=True)
    path.write_bytes(b'original frozen protocol\n')
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    plan = {'inputs': {}, 'code': {}, 'references': {str(original): expected}}
    (tmp_path / 'plan.json').write_text(json.dumps(plan))
    assert validate(tmp_path) == plan
    assert not original.exists()
    path.write_bytes(b'changed protocol\n')
    with pytest.raises(ValueError, match='Archived protocol changed'):
        validate(tmp_path)


def test_corrupt_source_protocol_cannot_be_hidden_by_an_intact_local_copy(tmp_path, monkeypatch):
    archive = tmp_path / 'protocols'
    archive.mkdir()
    monkeypatch.setattr(research_artifacts, 'LEGACY_PROTOCOLS', archive)
    original = archive / 'OLD_PLAN.md'
    original.write_bytes(b'original frozen protocol\n')
    expected = hashlib.sha256(original.read_bytes()).hexdigest()
    snapshot = tmp_path / 'source/audio_lab/OLD_PLAN.md'
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b'corrupted archive\n')
    with pytest.raises(ValueError, match='Archived protocol changed'):
        research_artifacts.legacy_protocol(tmp_path, original.name, expected)


def test_missing_protocol_and_unrelated_references_still_fail(tmp_path, monkeypatch):
    archive = tmp_path / 'protocols'
    archive.mkdir()
    monkeypatch.setattr(research_artifacts, 'LEGACY_PROTOCOLS', archive)
    with pytest.raises(FileNotFoundError, match='absent from local artifacts'):
        research_artifacts.legacy_protocol(tmp_path, 'MISSING.md', 'missing')
    unrelated = tmp_path / 'datasets/MISSING.md'
    (archive / unrelated.name).write_bytes(b'not the referenced document')
    (tmp_path / 'plan.json').write_text(json.dumps({
        'inputs': {}, 'code': {}, 'references': {str(unrelated): 'missing'}}))
    with pytest.raises(FileNotFoundError):
        validate(tmp_path)


def test_real_main_is_archived_and_changed_execution_is_rejected(tmp_path):
    # The registered research runner puts pytest's disposable fixtures below
    # experiments/, as required by the artifact boundary.
    assert tmp_path.is_relative_to(EXPERIMENTS)
    (tmp_path/'reservation.json').write_text('{}')
    (tmp_path/'input.txt').write_text('fixed input')
    entrypoint=tmp_path/'archive_probe.py'
    original='''from pathlib import Path
import sys
from experiments.audio_lab_tools.support import quality_round as evidence
root=Path(__file__).parent
evidence.RESERVED=root/'reservation.json'
evidence.reserve_memo=lambda: {}
directory=root/'round'
if sys.argv[1]=='freeze':
    evidence.freeze(directory,745,{'probe':True},{'data.txt':root/'input.txt'})
else:
    evidence.validate(directory,execution=sys.argv[1]=='execute')
'''
    entrypoint.write_text(original)
    environment={**os.environ,'PYTHONPATH':os.pathsep.join((str(tmp_path),str(EXPERIMENTS.parent)))}
    def command(action):
        return subprocess.run([sys.executable,'-B','-m','archive_probe',action],
            env=environment,cwd=EXPERIMENTS.parent,text=True,capture_output=True,timeout=30)
    frozen=command('freeze')
    assert frozen.returncode==0, frozen.stderr
    plan=json.loads((tmp_path/'round/plan.json').read_text())
    name=str(entrypoint.relative_to(EXPERIMENTS))
    assert name in plan['code']
    assert 'audio_lab_tools/__init__.py' in plan['code']
    assert 'audio_lab_tools/support/__init__.py' in plan['code']
    archived=tmp_path/'round/source'/name
    assert archived.read_text()==original
    assert command('execute').returncode==0
    entrypoint.write_text(original+'\n# Changed disposable orchestration after freeze.\n')
    changed=command('execute')
    assert changed.returncode!=0
    assert 'Executing code changed; freeze a new round: '+name in changed.stderr
    assert command('historical').returncode==0
    assert archived.read_text()==original
