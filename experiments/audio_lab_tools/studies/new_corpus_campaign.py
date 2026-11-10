"""Run the frozen new-audio pilot after the current checkpoint releases its lock."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

from ..support.external_experiment import OUT, ROOT, digest, save, stamp

WORK = OUT / 'checkpoint-1/new-corpora'
CHECKPOINT = OUT / 'checkpoint-1/status.json'
LAUNCHER = ROOT / 'experiments/run.py'
STEPS = (('check',), ('windows', 'audit'), ('windows', 'extract'), ('windows', 'transfer'))


def checkpoint_finished(current, started_at):
    if current.get('started_at') != started_at:
        raise ValueError('Checkpoint invocation changed while the pilot was waiting')
    return current.get('active') is None and current.get('state') in ('complete', 'partial')


def run(args):
    state = {'started_at': stamp(), 'state': 'starting', 'steps': []}
    status_path = WORK / 'campaign-status.json'
    # Refuse an unreviewed switch of code or the already frozen target plan.
    paths = [LAUNCHER, Path(__file__), WORK / 'current-plan.json',
             *sorted((ROOT / 'experiments/audio_lab_tools').rglob('test_*.py')),
             ROOT / 'experiments/audio_lab_tools/datasets/dataset_windows.py']
    hashes = {str(path): digest(path) for path in paths}
    state['pinned_sha256'] = hashes

    def record(**values):
        state.update(updated_at=stamp(), **values)
        save(status_path, state)

    try:
        current = json.loads(CHECKPOINT.read_text())
        started_at = current['started_at']
        state['checkpoint_started_at'] = started_at
        deadline = time.monotonic() + 8 * 3600
        while not checkpoint_finished(current, started_at):
            if not args.after_checkpoint:
                raise ValueError('Checkpoint has not finished; use --after-checkpoint to queue this pilot')
            if time.monotonic() >= deadline:
                raise TimeoutError('Checkpoint did not finish within the eight-hour wait budget')
            record(state='waiting_for_checkpoint', checkpoint_state=current.get('state'),
                   checkpoint_active=current.get('active'))
            time.sleep(30)
            current = json.loads(CHECKPOINT.read_text())
        record(state='ready', checkpoint_state=current['state'], checkpoint_active=None)
        for command in STEPS:
            if any(not Path(path).exists() or digest(path) != expected for path, expected in hashes.items()):
                raise ValueError('Queued pilot code or frozen plan changed; review and restart it')
            entry = {'command': list(command), 'state': 'running', 'started_at': stamp()}
            state['steps'].append(entry)
            record(state='running', active=list(command))
            print(json.dumps({'pilot_stage': command, 'state': 'running'}), flush=True)
            result = subprocess.run([str(LAUNCHER), *command], cwd=ROOT, check=False)
            entry.update(state='complete' if result.returncode == 0 else 'failed',
                         returncode=result.returncode, finished_at=stamp())
            record()
            if result.returncode:
                raise RuntimeError('Pilot stage failed: ' + ' '.join(command))
        record(state='complete', active=None, output_sha256={name: digest(WORK / name) for name in
            ('features.json', 'overlap-audit.json', 'transfer-report.json')})
    except BaseException as error:
        record(state='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', error=repr(error))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-checkpoint', action='store_true',
                        help='Wait at most eight hours for this invocation; take no machine test lock while waiting')
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / '.campaign.lock').open('a+') as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner.seek(0)
            raise SystemExit('A pilot coordinator already owns this campaign: ' + owner.read())
        owner.seek(0)
        owner.truncate()
        json.dump({'pid': os.getpid(), 'started_at': stamp(), 'cwd': str(ROOT)}, owner)
        owner.flush()
        run(args)


if __name__ == '__main__':
    main()
