"""Export the chosen Emotify expert and replay it without fitting or DB access.

Run: uv run python -m experiments.audio_lab_tools.export_emotify_product
The packaged artifact contains coefficients and aggregate provenance only.
"""
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_external as runtime
from yt_playlist.rec.audio_vibe_model import DEFAULT_PATH, _load

from .external_experiment import OUT, digest, save, stamp
from .external_export_check import replay
from .external_transfer import SNAPSHOT, probabilities

SOURCE = OUT / 'models/consensus-soundtracks-set2+cal500+emotify+pmemo.json'
REPORT = OUT / 'consensus-soundtracks-set2+cal500+emotify+pmemo.report.json'


def export():
    source = json.loads(SOURCE.read_text())
    report = json.loads(REPORT.read_text())
    snapshot = json.loads(SNAPSHOT.read_text())
    gold = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
    head = source['native_heads']['emotify']
    calibration = source['calibrators']['emotify']
    native = json.loads((OUT / 'models/emotify.embedding.json').read_text())
    if (source['report_sha256'] != digest(REPORT) or report['snapshot_sha256'] != digest(SNAPSHOT)
            or source['vibes'] != list(runtime.LABELS) or head['target_names'] != list(runtime.EMOTIONS)
            or head['selected_columns'] != list(range(len(runtime.EMOTIONS)))
            or calibration['feature_names'] != ['emotify:' + name for name in runtime.EMOTIONS]
            or head['excluded_ids'] or source['gold_training_ids'] != [row['video_id'] for row in gold]
            or len(report['embedding_signatures']) != 1
            or any(row['embedding_signature'] != report['embedding_signatures'][0] for row in gold)):
        raise ValueError('Chosen model no longer matches its evaluated inputs')
    keys = ('mean', 'scale', 'weights', 'intercept')
    artifact = {'format': runtime.FORMAT, 'feature_version': runtime.FEATURE_VERSION,
        'taxonomy_version': TAXONOMY_VERSION, 'labels': list(runtime.LABELS),
        'emotions': list(runtime.EMOTIONS), 'embedding_signature': report['embedding_signatures'][0],
        'native': {key: head['model'][key] for key in keys},
        'calibration': {key: calibration[key] for key in keys},
        'training': {'songs': len(gold), 'native_songs': len(native['training_ids']),
                     'objective': 'acceptable_nll', 'native_alpha': native['alpha'],
                     'calibration_alpha': calibration['alpha']},
        'evaluation': {**report['methods']['expert:emotify'], 'validation': report['validation'],
                       'status': 'exploratory_previously_examined_developer_set'},
        'provenance': {'research_export_sha256': digest(SOURCE), 'report_sha256': digest(REPORT),
            'snapshot_sha256': digest(SNAPSHOT), 'native_model_sha256': head['source_model_sha256'],
            'native_annotations_sha256': native['source_sha256'],
            'dataset': 'https://www2.projects.science.uu.nl/memotion/emotifydata/',
            'citation': 'A. Aljanaki, F. Wiering, R. C. Veltkamp. Studying emotion induced by music through a crowdsourcing game. Information Processing & Management, 2015.'}}
    runtime.validate(artifact)
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_PATH.write_text(json.dumps(artifact, separators=(',', ':'), allow_nan=False) + '\n')
    stat = DEFAULT_PATH.stat()
    loaded, artifact_hash = _load(str(DEFAULT_PATH), stat.st_mtime_ns, stat.st_size)
    # Real saved embeddings, in a compatible extractor envelope. No audio is
    # re-extracted and this replay does not estimate predictive performance.
    index = json.loads((OUT / 'emotify.features.json').read_text())['rows']
    envelope = json.loads(Path(index[0]['feature_file']).read_text())['prediction']
    envelope['artifact_hashes'] = {row['path']: row['sha256'] for row in envelope['bundle']['files']}
    x = np.array([row['embedding_values'] for row in gold])
    with threadpool_limits(limits=1):
        expected = probabilities(replay(calibration, replay(head['model'], x)))
        actual = []
        for row in gold:
            sample = {**envelope, 'embedding': {**envelope['embedding'], 'values': row['embedding_values']}}
            values = runtime.scores(loaded, [sample])
            actual.append([values[name] for name in runtime.LABELS])
        error = float(np.max(np.abs(expected - np.array(actual))))
    if error > 1e-10:
        raise ValueError(f'Product inference differs from research replay: {error}')
    validation = {'created_at': stamp(), 'artifact_sha256': artifact_hash,
        'artifact_bytes': stat.st_size, 'rows_replayed': len(gold),
        'maximum_absolute_replay_error': error, 'research_export_sha256': digest(SOURCE),
        'exporter_sha256': digest(__file__),
        'scope': 'Product JSON loading and both inference stages replayed against independent NumPy formulas on actual saved gold embeddings in a controlled compatible extractor envelope. No fitting, audio extraction, application startup or database access.',
        'evaluation': artifact['evaluation']}
    save(OUT / 'product-emotify-export.report.json', validation)
    print(json.dumps(validation), flush=True)
    return validation


if __name__ == '__main__':
    export()
