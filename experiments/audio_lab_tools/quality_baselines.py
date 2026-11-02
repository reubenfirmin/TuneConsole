"""Independent replay of exact frozen specialist coefficients for research."""
import base64
import hashlib
import json
import zlib

import numpy as np

from .mapped_vibe_inference import replay


def experts(artifact):
    if 'payload' not in artifact:
        return artifact['experts']
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(artifact['payload'], validate=True), 4 * 1024 ** 2)
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or hashlib.sha256(raw).hexdigest() != artifact['payload_sha256']:
        raise ValueError('Frozen Cruise payload changed or exceeds bounds')
    return json.loads(raw)


def predict(artifact, chunks, counts):
    native, scores = {}, {}
    vibe = artifact.get('vibe', 'cruise')
    for source, expert in experts(artifact).items():
        values = replay(expert['model'], chunks.reshape(-1, chunks.shape[-1])).reshape(*chunks.shape[:2], -1)
        averaged = np.asarray([v[:n].mean(0) for v, n in zip(values, counts, strict=True)])
        attributes = {k: averaged[:, i] for i, k in enumerate(expert['target_names'])}
        native[source] = attributes
        if vibe == 'cruise':
            scores[source] = (1 - 2 * np.abs(attributes['arousal'] - .5) + attributes['valence']) / 2
        elif vibe == 'relax' and source == 'emotify-artist':
            scores[source] = (attributes['calmness'] + 1 - attributes['tension']) / 2
        elif vibe == 'relax' and source == 'soundtracks-all':
            scores[source] = (2 - attributes['energy'] - attributes['tension'] + attributes['tender']) / 3
        elif vibe == 'relax':
            scores[source] = (1 - attributes['arousal'] + attributes['valence']) / 2
        else:
            scores[source] = np.mean(list(attributes.values()), axis=0)
    weights = artifact.get('weights', {s: 1 / len(scores) for s in scores})
    return sum(scores[s] * weights[s] for s in scores), native
