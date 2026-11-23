"""Capture optional models once, then run prediction against detached values."""
from yt_playlist.core import audio_embedding
from yt_playlist.rec import audio_vibe_runtime, audio_vibe_values
from yt_playlist.rec.audio_vibe_specialists import SPECIALISTS


def configured():
    """Bind independent specialist loaders without the library composition root."""
    def specialist(module):
        def load():
            path = module.configured_path()
            return module.load(path) if path else None
        return module.mode, load, module.score

    return VibePrediction(specialists=[(vibe, *specialist(module)) for vibe, module in SPECIALISTS])


class VibePrediction:
    def __init__(self, *, specialists):
        self.specialists = specialists

    def capture(self):
        specialists = []
        for vibe, mode, load, score in self.specialists:
            selected = mode()
            if selected == 'off':
                continue
            try:
                model = load()
            except audio_vibe_runtime.MODEL_ERRORS:
                continue
            if model is not None:
                specialists.append((vibe, selected, model, score))
        return PredictionValues(specialists)


class PredictionValues:
    def __init__(self, specialists=()):
        self.specialists = specialists
        self.signatures = {model['header']['embedding_signature']
                           for _, mode, model, _ in specialists if mode == 'active'}

    def usable_samples(self, samples):
        """Choose the largest set that an active specialist can score together."""
        compatible = {}
        for sample in samples:
            try:
                signature = audio_embedding.signature(sample)
            except ValueError:
                continue
            if signature in self.signatures:
                compatible.setdefault(signature, []).append(sample)
        return max(compatible.values(), key=len, default=[])

    def predict(self, samples, *, metadata=None, require_complete=True):
        if not samples:
            return None
        if len(samples) < 3 and (require_complete or not any(sample.get('embedding') for sample in samples)):
            return None
        result = {'suitability': {}, 'individual_models': {}}
        for vibe, mode, specialist, score in self.specialists:
            result = audio_vibe_runtime.apply_loaded(samples, result, selected_mode=mode,
                model=specialist, scorer=score, vibe=vibe)
        return audio_vibe_values.specialist_values(result, len(samples))
