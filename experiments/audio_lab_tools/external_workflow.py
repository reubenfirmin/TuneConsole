"""Run the existing real-library listening audit in a disposable research copy.

The established audit opens the source read-only, pins its SQLite read
transaction, and performs every migration and preparation operation on its copy.
Human listening answers stay blank; generated lists are review artifacts.
"""
from contextlib import redirect_stdout
import json
from pathlib import Path
import shutil
import tempfile

from pytest import MonkeyPatch

from .external_checkpoint import RUN
from .external_experiment import ROOT, digest, save, stamp


def runtime_hashes():
    paths = [*ROOT.joinpath('src/yt_playlist').rglob('*.py'),
             ROOT / 'experiments/investigations/bench_musical_character.py',
             ROOT / 'tests/bench_svd_background.py',
             ROOT / 'tests/musical_evaluation_support.py']
    return {str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)}


def evaluate(database, *, capture_seconds=120):
    from experiments.investigations.bench_musical_character import test_saved_catalog_musical_character_audit
    from yt_playlist.rec import audio_vibe_model

    before = runtime_hashes()
    target = RUN / ('listening-workflow-' + stamp().replace(':', '').replace('+', '_'))
    model = ROOT / 'src/yt_playlist/rec/data/emotify-vibes.json'
    frozen_model = RUN / ('workflow-comparator-' + digest(model)[:12] + '.json')
    if not frozen_model.exists():
        shutil.copyfile(model, frozen_model)
    details = {'created_at': stamp(), 'runtime_code_sha256': before,
        'model_sha256': digest(frozen_model), 'source_database': str(Path(database).resolve()),
        'output_directory': str(target), 'source_access': 'SQLite mode=ro; pinned backup transaction',
        'human_judgments': 0, 'limitations': [
            'Saved prepared state is retained before bounded continuing updates; this is not a fresh-user startup measurement.',
            'The stored publications are historical artifacts, not a paired replay of an older implementation.',
            'Generated Radio and Mix Tape lists need blind listening and manual identity review.',
            'No new external candidate is activated or compared in this workflow audit.']}
    RUN.mkdir(parents=True, exist_ok=True)
    log_path = RUN / 'listening-workflow.log'
    try:
        with tempfile.TemporaryDirectory(prefix='workflow-copy-', dir=RUN) as temporary:
            with MonkeyPatch.context() as patch, log_path.open('a') as log, redirect_stdout(log):
                patch.setenv('TUNECONSOLE_BENCH_DB', str(Path(database).resolve()))
                patch.setenv('TUNECONSOLE_MUSICAL_REPORT_DIR', str(target.resolve()))
                patch.setenv('TUNECONSOLE_MUSICAL_CAPTURE_SECONDS', str(capture_seconds))
                patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(frozen_model.resolve()))
                test_saved_catalog_musical_character_audit(Path(temporary), patch)
        details['audit'] = json.loads((target / 'report.json').read_text())
        details['state'] = 'complete'
    except BaseException as error:
        details.update(state='failed', error=repr(error))
        raise
    finally:
        details['runtime_code_unchanged_during_run'] = before == runtime_hashes()
        if target.exists():
            details['outputs'] = {str(path.relative_to(RUN)): digest(path)
                                  for path in target.glob('*.json')}
        save(RUN / 'listening-workflow-index.json', details)
    print(json.dumps({'listening_workflow': details['state'],
                      'output_directory': str(target),
                      'runtime_code_stable': details['runtime_code_unchanged_during_run']}), flush=True)
    return details
