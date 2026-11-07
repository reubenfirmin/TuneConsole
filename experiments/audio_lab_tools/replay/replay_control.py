"""Interrupt this checkout's own frozen paired replay without discarding reports."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal


ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'experiments/audio_lab/local/external-20261028/checkpoint-1/library-replay'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='Existing paired replay directory inside experiments/')
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    output_evaluation = directory.is_relative_to(ROOT / 'experiments/audio_lab/local/output-evaluation-587-20261030')
    if not output_evaluation and (not directory.is_relative_to(WORK) or not directory.name.startswith('paired-')):
        parser.error('Expected this checkout\'s private paired replay directory')
    plan = json.loads((directory / 'plan.json').read_text())
    bootstrap = str(ROOT / 'experiments/audio_lab_tools/replay/replay_runtime.py')
    required = {bootstrap, 'experiments.audio_lab_tools.replay.' + ('output_evaluation' if output_evaluation else 'library_replay')}
    matches = []
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            command = process.joinpath('cmdline').read_bytes().decode().split('\0')
            if (process / 'cwd').resolve() != ROOT or not required <= set(command):
                continue
            if not ({'freeze', 'run'} if output_evaluation else {'paired', 'paired-slice'}) & set(command):
                continue
            runtime = Path(command[command.index(bootstrap) + 1]).resolve()
            if output_evaluation:
                same_campaign = Path(command[command.index('--directory') + 1]).resolve() == directory
            elif '--resume' in command:
                same_campaign = Path(command[command.index('--resume') + 1]).resolve() == directory
            else:
                database = (Path(command[command.index('--database') + 1]) if '--database' in command
                            else Path('/home/rfirmin/.local/share/yt-playlist/state.db'))
                same_campaign = database.resolve() == Path(plan['database'])
            if runtime == Path(plan['runtime_snapshot']) and same_campaign:
                matches.append(int(process.name))
        except (OSError, UnicodeError, IndexError, ValueError):
            continue
    if len(matches) != 1:
        parser.error(f'Expected exactly one matching frozen replay process, found {len(matches)}')
    report = {'at': datetime.now(timezone.utc).isoformat(), 'pid': matches[0],
              'signal': 'SIGINT', 'reason': args.reason,
              'policy': 'Interrupt the owned research process; preserve partial reports and private cleanup.'}
    path = directory / 'stop-request.json'
    with path.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    path.chmod(0o600)
    os.kill(matches[0], signal.SIGINT)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
