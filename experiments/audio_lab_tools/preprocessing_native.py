"""Persistent independent C++ MusiCNN reference for the preprocessing study."""
import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

from .external_experiment import LAB


def embedding_vectors(values):
    values = np.asarray(values, dtype=float)
    if values.shape != (200,) or not np.isfinite(values).all():
        raise ValueError('Expected one finite 200-dimensional embedding')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(LAB / 'local/reference-python'))
    import essentia
    import essentia.standard as es

    frontend = es.TensorflowInputMusiCNN()
    predictor = es.TensorflowPredict(graphFilename=str(args.graph),
        inputs=['model/Placeholder'], outputs=['model/dense/BiasAdd'],
        isTrainingName='model/Placeholder_1')
    print('READY ' + json.dumps({'essentia': essentia.__version__,
        'policy': '512-sample frames, hop 256, nonoverlapping full 187-frame patches'}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        started = time.monotonic()
        signal = np.fromfile(request['pcm'], dtype='<f4')
        if signal.shape != (160000,) or not np.isfinite(signal).all():
            raise ValueError('Expected ten finite seconds of 16 kHz mono PCM')
        patches = ((len(signal) - 512) // 256 + 1) // 187
        features = np.asarray([frontend(signal[i * 256:i * 256 + 512])
                               for i in range(patches * 187)], dtype=np.float32)
        pool = essentia.Pool()
        pool.set('model/Placeholder', features.reshape(patches, 1, 187, 96))
        output = np.asarray(predictor(pool)['model/dense/BiasAdd']).reshape(patches, 200)
        vector = embedding_vectors(output.mean(axis=0))
        print('RESULT ' + json.dumps({'id': request['id'], 'embedding': vector.tolist(),
            'patches': patches, 'wall_seconds': time.monotonic() - started}), flush=True)


if __name__ == '__main__':
    main()
