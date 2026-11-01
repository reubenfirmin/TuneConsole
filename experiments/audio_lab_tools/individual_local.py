"""Freeze current human labels and captures read-only for specialist diagnostics."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import LAB, digest, save, stamp

DEFAULT = LAB / 'local/individual-20261030/local-1'
OLD = LAB / 'local/mapped-vibes-20261029/cruise-baselines-1'


def export(directory, database):
    from . import train_vibes
    from yt_playlist.core import audio_embedding, sample_files
    if directory.exists():
        raise ValueError('Preserve the previous local export; choose a new directory')
    paths = {Path(module.__file__).resolve() for name, module in list(sys.modules.items())
             if name.startswith(('yt_playlist.', 'experiments.')) and getattr(module, '__file__', None)}
    sources = {str(path): digest(path) for path in paths if path.is_file()}
    rows, skipped = train_vibes.examples_from_db(database, require_embeddings=True)
    old_rows = [row for name in ('development', 'reserved')
                for row in json.loads((OLD / f'inputs/{name}.json').read_text())['rows']]
    old = {row['video_id']: row for row in old_rows}
    directory.mkdir(parents=True)
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        files = dict(conn.execute('SELECT id,filename FROM sample_attributes'))
    outputs, selected = {}, []
    for original in sorted(rows, key=lambda row: row['video_id']):
        row = {key: value for key, value in original.items()
               if key not in ('feedback_prediction', 'values', 'baseline')}
        samples, copied = [], []
        for sample_id in row['sample_ids']:
            source = Path(files[sample_id])
            value = sample_files.read(source, expected={'id': sample_id, 'video_id': row['video_id']})
            checksum = digest(source)
            relative = 'samples/' + checksum + ''.join(source.suffixes)
            target = directory / relative
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                shutil.copyfile(source, target)
            if digest(target) != checksum:
                raise ValueError('Capture changed during copy')
            samples.append(value)
            copied.append(relative)
            outputs[relative] = checksum
        if not np.allclose(audio_embedding.vector(samples), row['embedding_values'], atol=1e-12, rtol=0):
            raise ValueError('Copied captures differ from the exported transaction')
        row.update(sample_files=copied, id=row['video_id'], group=row['video_id'],
                   prior_inspected=row['video_id'] in old,
                   label_changed_since_570=(row['video_id'] in old and set(row['labels']) != set(old[row['video_id']]['labels'])))
        selected.append(row)
    save(directory / 'labels.json', {'rows': selected, 'skipped': skipped})
    identities = [{k: row[k] for k in ('id', 'group', 'video_id', 'artist', 'title')} for row in selected]
    save(directory / 'identities.json', identities)
    outputs.update({name: digest(directory / name) for name in ('labels.json', 'identities.json')})
    if any(digest(Path(path)) != checksum for path, checksum in sources.items()):
        raise ValueError('Exporter dependency changed while freezing; preserve this attempt')
    save(directory / 'receipt.json', {'created_at': stamp(), 'outputs': outputs, 'source': sources,
         'database': str(database), 'access': 'read-only SQL transaction, no Store or migration',
         'rows': len(selected), 'new_identities_since_570': sum(r['video_id'] not in old for r in selected),
         'labels_are_supplementary': True, 'unselected_vibes': 'unknown',
         'selection': 'latest valid current-taxonomy annotation and compatible latest capture per point',
         'exposure_limit': 'Prior inspection flag covers #570; other earlier source/training exposure requires per-model exclusion.'})
    print(json.dumps({k: v for k, v in json.loads((directory / 'receipt.json').read_text()).items()
                      if k not in ('outputs', 'source')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--database', type=Path, default=Path('/home/rfirmin/.local/share/yt-playlist/state.db'))
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(LAB / 'local'):
        parser.error('Keep exports under experiments/audio_lab/local')
    with threadpool_limits(limits=1):
        export(directory, args.database)


if __name__ == '__main__':
    main()
