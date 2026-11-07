"""Versioned human judgments, separate from product vibe calculations."""
import math

FILE_FORMAT = 'audio-annotation-1'
LEGACY_FEEDBACK_TAXONOMY = 'audio-feedback-presets-1'
TAXONOMIES = {
    LEGACY_FEEDBACK_TAXONOMY: ('gentle', 'boost', 'focus', 'bright', 'unwind', 'move'),
    'audio-session-targets-1': ('relax', 'concentrate', 'cruise', 'release', 'energize', 'uplift'),
}
KINDS = ('feedback', 'development', 'session')
STATES = ('labeled', 'empty', 'withdrawn')


def validate(run_id, samples, labels, now, *, kind='session', state=None):
    if kind not in KINDS or not isinstance(run_id, str) or not run_id:
        raise ValueError('Invalid annotation owner or kind')
    samples, labels = list(samples), list(labels)
    state = state or ('labeled' if labels else 'empty')
    if (state not in STATES or (state == 'labeled') != bool(labels)
            or any(not isinstance(value, str) or not value for value in [*samples, *labels])
            or len(set(samples)) != len(samples) or len(set(labels)) != len(labels)
            or (kind == 'feedback' and len(labels) > 1)):
        raise ValueError('Invalid annotation samples, labels or state')
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('Invalid annotation time')
    return samples, labels, state


def evaluation_values(prediction, vocabulary):
    """Retain typed evaluation only when the captured output is complete and valid."""
    if not isinstance(prediction, dict) or not vocabulary:
        return None
    try:
        ranking, predicted = prediction['ranking'], prediction['vibes']
        scores = {row['vibe']: row['score'] for row in ranking}
        if (len(ranking) != len(vocabulary) or set(scores) != set(vocabulary)
                or not all(isinstance(score, (float, int)) and not isinstance(score, bool)
                           and math.isfinite(score) and 0 <= score <= 1 for score in scores.values())
                or not isinstance(predicted, list) or not predicted or len(set(predicted)) != len(predicted)
                or not set(predicted) <= set(vocabulary)):
            return None
        captured_model = prediction.get('model')
        model = {} if captured_model is None else captured_model
        if not isinstance(model, dict):
            return None
        trained = model.get('trained_labels', [])
        if (not isinstance(trained, list) or len(set(trained)) != len(trained)
                or not set(trained) <= set(vocabulary)):
            return None
        training = model.get('training_songs')
        if training is not None and (type(training) is not int or training < 0):
            return None
        text = [prediction.get('version'), model.get('sha256'), model.get('feature_version')]
        if any(value is not None and not isinstance(value, str) for value in text):
            return None
    except (KeyError, TypeError):
        return None
    return dict(header=(prediction.get('taxonomy_version'), text[0], int(captured_model is not None),
                        int('trained_labels' in model), *text[1:], training),
                scores=[(item['vibe'], position, item['score']) for position, item in enumerate(ranking)],
                predicted=list(predicted), trained=list(trained))
