"""Sequential external-data and learner campaign, isolated from the application."""
import fcntl
import json
import os
import traceback

from .external_experiment import DATA, OUT, digest, extract, save, stamp
from .external_sources import stage_jamendo

STATUS = OUT / 'campaign-status.json'
BASE = ['soundtracks-set2', 'cal500', 'emotify', 'pmemo', 'vgmidi-original']


def main():
    from .external_consensus import evaluate as consensus
    from .external_learners import evaluate as learners
    from .external_results import write_summary
    from .external_training import evaluate
    from .external_transfer import evaluate_transfer
    previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    state = {'started_at': previous.get('started_at', stamp()), 'state': 'running',
             'steps': previous.get('steps', []), 'script_sha256': digest(__file__), 'pid': os.getpid()}
    for row in state['steps']:
        if row['state'] == 'running':
            row.update(state='interrupted', finished_at=stamp())

    def run(name, operation):
        completed = next((row for row in state['steps'] if row['name'] == name and row['state'] == 'complete'), None)
        if completed:
            return True
        row = {'name': name, 'state': 'running', 'started_at': stamp()}
        state['steps'].append(row)
        state['active'] = name
        state['updated_at'] = stamp()
        save(STATUS, state)
        print(json.dumps({'campaign_step': name, 'state': 'running', 'at': stamp()}), flush=True)
        try:
            operation()
            row['state'] = 'complete'
        except KeyboardInterrupt:
            row.update(state='interrupted', finished_at=stamp())
            state.update(state='interrupted', updated_at=stamp())
            save(STATUS, state)
            raise
        except Exception as error:
            row.update(state='failed', error=repr(error), traceback=traceback.format_exc())
            print(traceback.format_exc(), flush=True)
        row['finished_at'] = stamp()
        state['updated_at'] = stamp()
        save(STATUS, state)
        write_summary()
        return row['state'] == 'complete'

    def native(dataset):
        extract(dataset, native=True)
        evaluate(dataset)
        learners(dataset)

    def transfer(datasets):
        evaluate_transfer(datasets)
        consensus(datasets)
        learners('gold-vibes', datasets)

    # Previous independent queues have already finished; no competing extractor is started.
    for dataset in ('emotify', 'pmemo', 'soundtracks-all', 'cal500', 'vgmidi-original'):
        run('learners:' + dataset, lambda dataset=dataset: learners(dataset))
    run('transfer:soundtracks-all-replacement',
        lambda: transfer(['soundtracks-all', *BASE[1:]]))
    if run('native:jamendo-human-00', lambda: native('jamendo-human-00')):
        run('transfer:jamendo-human-00', lambda: transfer([*BASE, 'jamendo-human-00']))
    # Downloads run independently of training. A failed or incomplete archive is
    # recorded rather than silently training on a partly downloaded collection.
    def stage_expanded():
        for shard in ('07', '23', '53'):
            stage_jamendo((shard,), remove_archive=True)
    if run('acquire:jamendo-expanded', stage_expanded):
        if run('native:jamendo-human-4shards', lambda: native('jamendo-human-4shards')):
            run('transfer:jamendo-expanded', lambda: transfer([*BASE, 'jamendo-human-4shards']))
    for dataset in ('coimbra-mirex-like', '4q-emotion', 'deam'):
        if run('native:' + dataset, lambda dataset=dataset: native(dataset)):
            run('transfer:' + dataset, lambda dataset=dataset: transfer([*BASE, dataset]))
    completed_sources = [dataset for dataset in ('coimbra-mirex-like', '4q-emotion', 'deam', 'jamendo-human-4shards')
                         if any(row['name'] == 'native:' + dataset and row['state'] == 'complete'
                                for row in state['steps'])]
    if completed_sources:
        all_sources = ['soundtracks-all', *BASE[1:], *completed_sources]
        run('transfer:all-new-audio', lambda: transfer(all_sources))
        for native_learner in ('ridge', 'rbf', 'selected_learner'):
            for calibrator in ('linear', 'rbf', 'softmax'):
                if native_learner == 'ridge' and calibrator == 'linear':
                    continue
                run('consensus:' + native_learner + ':' + calibrator,
                    lambda native_learner=native_learner, calibrator=calibrator:
                    consensus(all_sources, native_learner, calibrator))
    latest = {row['name']: row['state'] for row in state['steps']}
    state['state'] = 'complete_with_failures' if any(value != 'complete' for value in latest.values()) else 'complete'
    state['active'] = None
    state['updated_at'] = stamp()
    save(STATUS, state)
    write_summary()


if __name__ == '__main__':
    with (OUT / 'campaign.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
