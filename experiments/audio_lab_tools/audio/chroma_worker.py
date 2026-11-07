"""Pinned, single-thread CQT summaries; no model fitting or label access."""
import json
from pathlib import Path
import resource
import sys
import time

import librosa
import numpy as np
import scipy


def summarize(chroma):
    return np.concatenate([value for order in range(3)
        for value in (np.diff(chroma, n=order, axis=1).mean(axis=1),
                      np.diff(chroma, n=order, axis=1).std(axis=1))])


def main():
    print('READY ' + json.dumps({'python': sys.version, 'numpy': np.__version__,
        'scipy': scipy.__version__, 'librosa': librosa.__version__}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        signal = np.fromfile(request['pcm'], dtype='<f4')
        if signal.shape != (160000,) or not np.isfinite(signal).all():
            raise ValueError('Expected exact finite ten-second PCM')
        started = time.monotonic()
        chroma = librosa.feature.chroma_cqt(y=signal, sr=16000, hop_length=512,
            fmin=32.70319566257483, n_chroma=12, n_octaves=7, bins_per_octave=36,
            tuning=0., norm=np.inf, threshold=0., cqt_mode='full')
        vector = summarize(chroma)
        if vector.shape != (72,) or not np.isfinite(vector).all():
            raise ValueError('Invalid chroma summary')
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        if rss > 2 * 1024**3:
            raise RuntimeError('Chroma RSS budget exceeded')
        print('RESULT ' + json.dumps({'id': request['id'], 'embedding': vector.tolist(),
            'seconds': time.monotonic() - started, 'frames': chroma.shape[1],
            'peak_rss_bytes': rss}), flush=True)


if __name__ == '__main__':
    main()
