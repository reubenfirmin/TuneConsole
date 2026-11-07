"""Research routing adapter around the production Cruise scorer.

Historical packages retain their archived runtime. New experiments use the same
inference implementation as the application.
"""
from statistics import mean

from yt_playlist.rec import audio_vibe_external as shared
from yt_playlist.rec import audio_vibe_runtime as bounded
from yt_playlist.rec import audio_vibe_cruise as production
from yt_playlist.rec.audio_vibe_cruise import (
    FORMAT, MAX_BYTES, MAX_PAYLOAD, MAX_EXCERPTS, SOURCES,
    audio_embedding, load, score,
)


def route(group, values, signature, *, specialist_path=None, mode='shadow'):
    """Replay selected-vibe routing without comparing mixed scales by argmax."""
    if mode not in ('off', 'shadow', 'active'):
        raise ValueError('Unknown Cruise routing mode')
    shared.validate(group)
    if signature != group['embedding_signature']:
        raise ValueError('Incompatible shared embedding signature')
    values = bounded.chunks(values)
    embedding = [mean(column) for column in zip(*values, strict=True)]
    emotions = shared._predict(group['native'], embedding)
    calibrated = [max(v, 1e-8) for v in shared._predict(group['calibration'], emotions)]
    total = sum(calibrated)
    baseline = dict(zip(shared.LABELS, (v / total for v in calibrated), strict=True))
    result = {'shared_scores': baseline, 'suitability': dict(baseline),
              'shared_top_vibe': max(shared.LABELS, key=baseline.get), 'mode': mode,
              'individual_models': {}, 'applied_vibes': []}
    if mode == 'off' or specialist_path is None:
        return result
    try:
        model = load(specialist_path)
        if model['header']['embedding_signature'] != signature:
            return result
        candidate = score(model, values)
    except bounded.MODEL_ERRORS:
        return result
    result['individual_models']['cruise'] = candidate
    if mode == 'active':
        result['suitability']['cruise'] = candidate['score']
        result['applied_vibes'] = ['cruise']
    return result


def route_samples(group, samples, *, specialist_path=None, mode='shadow'):
    """Validate every excerpt's extractor identity before research replay."""
    if not samples or len(samples) > MAX_EXCERPTS:
        raise ValueError('Missing or excessive audio excerpts')
    signatures = {audio_embedding.signature(sample) for sample in samples}
    if len(signatures) != 1:
        raise ValueError('Mixed audio embedding provenance')
    return route(group, [sample['embedding']['values'] for sample in samples], signatures.pop(),
                 specialist_path=specialist_path, mode=mode)
