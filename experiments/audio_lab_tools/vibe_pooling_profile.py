"""Diagnostic posterior-load profile on a manager-owned private library.

Fixed variances isolate preparation overhead; these timings are not candidate
quality, fitting performance or foreground acceptance evidence.
"""
import argparse
import cProfile
import hashlib
import io
import json
import os
from pathlib import Path
import pstats
import time
import zipfile

from yt_playlist.core.activity import work_budget
from yt_playlist.core.store import Store
from yt_playlist.rec import vibe_joint_model as model, vibe_joint_preparation as preparation


def run(output):
    if (os.environ.get('TUNECONSOLE_IMAGE_RUN') != '1'
            or os.environ.get('TUNECONSOLE_TEST_LIBRARY') != '/library/state.db'
            or output.parent.resolve() != Path('/reports')):
        raise ValueError('Run through test-library.py on a private writable snapshot')
    repository = Path(__file__).resolve().parents[2]
    sources = ('experiments/audio_lab_tools/vibe_pooling_profile.py',
        'src/yt_playlist/core/vibe_model_files.py', 'src/yt_playlist/repos/vibe_models.py',
        'src/yt_playlist/rec/vibe_joint_model.py', 'src/yt_playlist/rec/vibe_joint_preparation.py')
    report = {'quality_acceptance': False, 'fixed_variances': model.INITIAL, 'code': {}}
    with zipfile.ZipFile(output.parent / 'source.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in sources:
            raw = (repository / name).read_bytes()
            archive.writestr(name, raw)
            report['code'][name] = hashlib.sha256(raw).hexdigest()
    store = Store('/library/state.db')
    previous_fit, previous_qualification = model.fit, preparation.QUALIFICATIONS
    try:
        store.init_schema()
        preparation.QUALIFICATIONS = {preparation.config_identity(): 'isolated-load-profile'}
        model.fit = lambda rows, vibe, **kwargs: previous_fit(rows, vibe, fixed=model.INITIAL, **kwargs)
        with work_budget(60):
            assert preparation.step(store, seconds=60)
        report['state'] = preparation.state(store, 'relax')
        assert report['state']['outcome'] == 'ready'
        profile, started = cProfile.Profile(), time.monotonic()
        with work_budget(60), store.read_snapshot() as snapshot:
            profile.enable()
            generation, posterior = preparation.posterior(snapshot.store, 'relax')
            profile.disable()
        report['posterior_seconds_including_profiler'] = time.monotonic() - started
        report['generation'], report['originals'] = generation, len(posterior.rows)
        profile.dump_stats(str(output.parent / 'posterior.prof'))
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).sort_stats('cumulative').print_stats(35)
        (output.parent / 'posterior.txt').write_text(stream.getvalue())
        report['source_pins_unchanged'] = all(hashlib.sha256((repository / name).read_bytes()).hexdigest() == digest
                                             for name, digest in report['code'].items())
    finally:
        model.fit, preparation.QUALIFICATIONS = previous_fit, previous_qualification
        store.conn.close()
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    run(parser.parse_args().output)
