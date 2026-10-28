"""Normal audio-metadata collection, with separately gated developer labels."""
import logging
import math
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from yt_playlist.core.features import audio_vibes_enabled
from yt_playlist.core import audio_embedding
from yt_playlist.rec import audio_vibe_feedback, audio_vibe_preview, home_vibe

logger = logging.getLogger(__name__)

Number = Annotated[float, Field(allow_inf_nan=False, ge=-100, le=100)]
Time = Annotated[float, Field(allow_inf_nan=False, ge=0, le=86400)]
Tag = Annotated[str, Field(min_length=1, max_length=64)]
Score = Annotated[float, Field(allow_inf_nan=False, ge=0, le=1)]
Vibe = Literal['relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release']


class Metadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Trait(Metadata):
    score: Number
    per_patch: Annotated[list[Number], Field(min_length=1, max_length=8)]

    @model_validator(mode='after')
    def mean_matches(self):
        if not math.isclose(self.score, sum(self.per_patch) / len(self.per_patch), abs_tol=.0001):
            raise ValueError('Trait mean must match its patches')
        return self


Scalar = Annotated[float, Field(allow_inf_nan=False, ge=-1e12, le=1e12)]
Positive = Annotated[float, Field(allow_inf_nan=False, ge=0, le=1e12)]


class AttributeValues(Metadata):
    integrated_lufs: Scalar | None
    momentary_lufs_mean: Scalar | None
    short_term_lufs_mean: Scalar | None
    excerpt_loudness_range_lu: Positive | None
    tempo_bpm: Annotated[float, Field(ge=40, le=208, allow_inf_nan=False)] | None
    tempo_confidence: Annotated[float, Field(ge=0, le=5.33, allow_inf_nan=False)] | None
    beat_interval_cv: Positive | None
    beat_count: Annotated[int, Field(ge=0, le=1000)]
    rms: Positive
    peak: Positive
    rms_dbfs: Scalar | None
    peak_dbfs: Scalar | None
    crest_factor: Positive | None
    peak_normalized_rms_dbfs: Scalar | None
    clipped_fraction: Score
    zero_crossings_per_second: Annotated[float, Field(ge=0, le=44100, allow_inf_nan=False)]
    spectral_centroid_hz: Positive | None
    spectral_flatness: Positive | None
    spectral_rolloff_hz: Positive | None
    spectral_spread_hz: Positive | None
    spectral_slope: Scalar | None
    spectral_skewness: Scalar | None
    spectral_kurtosis: Scalar | None
    spectral_crest: Positive | None
    perceptual_spread: Positive | None
    perceptual_sharpness: Positive | None
    bark_loudness: Positive | None


class AudioAttributes(Metadata):
    version: Literal['audio-attributes-1']
    sample_rate: Literal[44100]
    channels: Literal[1, 2]
    seconds: Literal[10]
    runtime: Literal['essentia-csp-audio-2']
    meyda_version: Literal['5.6.3']
    scope: Literal['excerpt']
    values: AttributeValues


class Embedding(Metadata):
    version: Literal['musicnn-embedding-1']
    layer: Literal['model/dense/BiasAdd']
    graph_policy: Literal['restore-fused-preactivation-1']
    pooling: Literal['mean_full_patches']
    patches: Annotated[int, Field(ge=1, le=8)]
    values: Annotated[list[Annotated[float, Field(allow_inf_nan=False, ge=-1e6, le=1e6)]],
                      Field(min_length=200, max_length=200)]


class Sample(Metadata):
    id: UUID
    run_id: UUID
    point: Literal['thirty', 'middle', 'two_thirds']
    capture_reason: Literal['scheduled', 'catch_up'] = 'scheduled'
    target_seconds: Time
    position_seconds: Time
    source_position_seconds: Time | None
    duration_seconds: Time | None
    video_id: Annotated[str, Field(min_length=1, max_length=128)]
    title: Annotated[str, Field(max_length=512)]
    artist: Annotated[str, Field(max_length=512)]
    metadata_verified: bool
    source_identity_verified: Literal[False] = False
    recommendation_adoption: Literal[False] = False
    raw_audio_retained: Literal[False] = False
    analysis_location: Literal['youtube_tab']
    audio_transferred_to_app: Literal[False]
    analysed_seconds: Annotated[float, Field(ge=8, le=10, allow_inf_nan=False)]
    audio_attributes: AudioAttributes | None = None
    embedding: Embedding | None = None
    tags: Annotated[dict[Tag, Score], Field(min_length=50, max_length=50)]
    traits: Annotated[dict[Tag, Trait], Field(min_length=6, max_length=6)]
    model: Annotated[str, Field(min_length=1, max_length=128)]
    model_version: Annotated[str, Field(min_length=1, max_length=64)]
    mood_versions: Annotated[dict[Tag, Annotated[str, Field(max_length=64)]], Field(max_length=5)]
    artifact_hashes: Annotated[dict[Annotated[str, Field(max_length=160)],
                                   Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]], Field(max_length=50)]

    @model_validator(mode='after')
    def check_traits_and_clock(self):
        if set(self.traits) != {'relaxed', 'aggressive', 'happy', 'sad', 'arousal', 'valence'}:
            raise ValueError('Expected the six native mood outputs')
        for name in ('relaxed', 'aggressive', 'happy', 'sad'):
            if any(not 0 <= number <= 1 for number in self.traits[name].per_patch):
                raise ValueError('Invalid binary mood output')
        if self.capture_reason == 'scheduled' and abs(self.position_seconds - self.target_seconds) > 5:
            raise ValueError('Excerpt missed its scheduled position')
        if self.capture_reason == 'catch_up' and self.position_seconds < self.target_seconds:
            raise ValueError('A replacement excerpt cannot precede its scheduled point')
        target = 30 if self.point == 'thirty' else (
            self.duration_seconds * (.5 if self.point == 'middle' else 2 / 3)
            if self.duration_seconds is not None else None)
        if target is None or abs(target - self.target_seconds) > .1:
            raise ValueError('Invalid scheduled sampling point')
        if self.duration_seconds is not None and self.position_seconds + 10 > self.duration_seconds + .5:
            raise ValueError('Excerpt does not fit within this song')
        if set(self.mood_versions) != {'relaxed', 'aggressive', 'happy', 'sad', 'deam'}:
            raise ValueError('Expected provenance for all five mood models')
        if self.embedding is not None:
            audio_embedding.signature(self.model_dump())
            seconds = ((self.embedding.patches * 187 - 1) * 256 + 512) / 16000
            if (not math.isclose(self.analysed_seconds, seconds, abs_tol=.0001)
                    or any(len(trait.per_patch) != self.embedding.patches for trait in self.traits.values())):
                raise ValueError('Embedding coverage must match the captured prediction patches')
        return self


class Playback(Metadata):
    run_id: UUID
    video_id: Annotated[str, Field(min_length=1, max_length=128)]
    title: Annotated[str, Field(max_length=512)]
    artist: Annotated[str, Field(max_length=512)]


class Feedback(Metadata):
    run_id: UUID
    sample_ids: Annotated[list[UUID], Field(max_length=3)]
    taxonomy_version: Literal['audio-session-targets-1']
    vibes: Annotated[list[Vibe], Field(max_length=6)]

    @model_validator(mode='after')
    def unique_vibes(self):
        if len(self.vibes) != len(set(self.vibes)):
            raise ValueError('Choose each vibe at most once')
        return self


class Failure(Metadata):
    run_id: UUID
    point: Literal['thirty', 'middle', 'two_thirds']
    attempt: Annotated[int, Field(ge=1, le=10000)]
    phase: Literal['capturing', 'analysing', 'saving']
    message: Annotated[str, Field(min_length=1, max_length=512)]


def require_enabled():
    if not audio_vibes_enabled():
        raise HTTPException(404)


async def parse(request, schema):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, 'Vibe metadata exceeded its limit')
    try:
        # JSON mode accepts UUID strings while strict mode rejects coerced numbers.
        value = schema.model_validate_json(bytes(body)).model_dump(mode='json')
        # An absent optional embedding must not change the content address of a
        # legacy capture retried after this API upgrade.
        if schema is Sample and value.get('embedding') is None:
            value.pop('embedding', None)
        return value
    except (ValidationError, ValueError):
        raise HTTPException(422, 'Invalid vibe metadata') from None


def build(ctx):
    router = APIRouter()
    ctx.templates.env.globals['audio_vibes_enabled'] = audio_vibes_enabled
    choices = {
        key: {'label': audio_vibe_preview.LABELS[key],
              'attribute_label': audio_vibe_preview.ATTRIBUTE_LABELS[key],
              'description': preset['description']}
        for key, preset in home_vibe.PRESETS.items()}
    ctx.templates.env.globals['audio_vibe_choices'] = choices
    ctx.templates.env.globals['audio_vibe_taxonomy_version'] = audio_vibe_preview.TAXONOMY_VERSION

    def state(run_id, video_id=None):
        samples = ctx.store.audio_vibes.samples(run_id)
        playback = ctx.store.audio_vibes.playback(run_id)
        if not audio_vibes_enabled():
            # Collection needs acknowledgements, not the developer's annotation
            # history or full diagnostic payloads. Preparation predicts off-thread.
            return {'run_id': run_id, 'playback': playback,
                    'samples': [{key: sample[key] for key in ('id', 'point')} for sample in samples],
                    'taxonomy_version': audio_vibe_preview.TAXONOMY_VERSION}
        metadata = ctx.store.audio_vibes.base_metadata(samples[0]['video_id']) if samples else None
        video_id = playback['video_id'] if playback else video_id
        feedback = ctx.store.audio_vibes.feedback(run_id)
        previous = (ctx.store.audio_vibes.latest_feedback(video_id, exclude_run_id=run_id)
                    if video_id and feedback is None else None)
        return {'run_id': run_id, 'playback': playback,
                'samples': samples, 'choices': choices,
                'taxonomy_version': audio_vibe_preview.TAXONOMY_VERSION,
                'prediction': audio_vibe_preview.predict(samples, metadata=metadata),
                'feedback': feedback, 'previous_feedback': previous,
                'legacy_feedback': ctx.store.audio_vibes.earlier_feedback(run_id)}

    @router.post('/audio-vibes/runs')
    async def started(request: Request):
        value = await parse(request, Playback)
        try:
            ctx.store.audio_vibes.save_playback(**value, now=ctx.now())
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return state(value['run_id'])

    @router.get('/audio-vibes/runs/{run_id}')
    def run(run_id: str, video_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None):
        try:
            run_id = str(UUID(run_id))
        except ValueError:
            raise HTTPException(422, 'Invalid playback run') from None
        return state(run_id, video_id)

    @router.post('/audio-vibes/samples')
    async def sample(request: Request):
        value = await parse(request, Sample)
        try:
            ctx.store.audio_vibes.save_sample(value, ctx.now())
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return state(value['run_id'])

    @router.post('/audio-vibes/feedback')
    async def feedback(request: Request):
        require_enabled()
        value = await parse(request, Feedback)
        try:
            audio_vibe_feedback.save(ctx.store, value['run_id'], value['sample_ids'], value['vibes'],
                                     value['taxonomy_version'], ctx.now())
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return state(value['run_id'])

    @router.post('/audio-vibes/failures')
    async def failure(request: Request):
        value = await parse(request, Failure)
        logger.warning('Audio vibe attempt failed: run=%s point=%s attempt=%s phase=%s message=%r',
                       value['run_id'], value['point'], value['attempt'], value['phase'], value['message'])
        return {'recorded': True}

    return router
