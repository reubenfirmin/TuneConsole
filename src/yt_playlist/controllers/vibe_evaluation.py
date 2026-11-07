"""Original-evidence evaluation and masking over detached repository inputs."""
import json

from yt_playlist.core import audio_vibe_metadata, vibe_manifest
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.evidence_ledger import canonical


class VibeEvaluation:
    def __init__(self, repository, sources, content, genres):
        self.repository, self.sources = repository, sources
        self.content, self.genres = content, genres

    def annotations(self, historical_videos=(), *, limit=10000, max_bytes=16 * 1024 * 1024):
        rows = self.repository.annotation_rows(limit)
        if len(rows) > limit:
            raise WorkBudgetExceeded('Annotation inventory exceeds bounded admission')
        revisions, videos, used = [], set(historical_videos), 0
        if len(videos) > limit:
            raise WorkBudgetExceeded('Historical label identities exceed bounded admission')
        for row in rows:
            check_stopping()
            values = self.repository.annotation_values(row['id'], row['run_id'] or row['legacy_run_key'])
            item = {**row, **values, 'videos': sorted(set(values['videos']))}
            videos.update(item['videos'])
            used += len(canonical(item))
            if used > max_bytes or len(videos) > limit:
                raise WorkBudgetExceeded('Annotation inventory exceeds bounded admission')
            revisions.append(item)
        identities = self.repository.video_recordings(sorted(videos))
        for item in revisions:
            recordings = {identities[video] for video in item['videos'] if video in identities}
            # Every upload must resolve to the same exact recording.
            item['recording'] = next(iter(recordings)) if len(recordings) == 1 and all(
                video in identities for video in item['videos']) else None
        return {'format': 'typed-vibe-annotations-1', 'revisions': revisions, 'video_recordings': identities}

    def conditioning(self, recordings):
        recordings = sorted(set(recordings))
        rows = self.repository.conditioning_rows(recordings, artist_limit=17)
        albums = vibe_manifest.albums(self.repository.album_facts(recordings))
        axes = self.genres.feature_axes([row['genre'] for row in rows.values() if row['genre']])
        return {str(recording): {
            'recording': recording, 'artists': row['artists'] if len(row['artists']) <= 16 else [],
            'genre': audio_vibe_metadata.subgenre({'genre': row['genre']}, genre_axes=axes),
            'album': albums.get(recording),
        } for recording, row in rows.items()}

    def training_manifest(self, vibe, *, limit=32768, max_bytes=8 * 1024 * 1024):
        rows = self.repository.training_rows(vibe, limit)
        if len(rows) > limit:
            raise WorkBudgetExceeded('Joint vibe original manifest exceeds bounded admission')
        albums = self.repository.album_facts([row['recording_id'] for row in rows])
        return vibe_manifest.training(rows, albums, limit=limit, max_bytes=max_bytes)

    def profile_inputs(self, recordings, *, max_bytes=32 * 1024 * 1024):
        """Keep artist/release descriptions and categorical genre choices only."""
        identities, result, used = sorted(set(recordings)), {}, 0
        for start in range(0, len(identities), 32):
            check_stopping()
            batch = identities[start:start + 32]
            keys = self.repository.recording_keys(batch)
            if set(keys.values()) != set(batch):
                raise ValueError('Evaluation profile requires each original recording')
            inputs = self.sources.recording_inputs(list(keys), source_scopes=('artist', 'release'))
            rows = self.content.capture([(key, 0) for key in keys], mask_numeric=True)
            for key, _revision, raw, _digest, _different in rows:
                payload = json.loads(raw)
                for features in payload.values():
                    if features is not None:
                        features['audio'] = {}
                profiles, overrides, truncated = inputs[key]
                value = {'content': payload,
                    'sources': [source for source in profiles if source['scope'] in {'artist', 'release'}],
                    'overrides': {field: value for field, value in overrides.items() if field == 'genre'},
                    'truncated': truncated}
                used += len(canonical(value))
                if used > max_bytes:
                    raise WorkBudgetExceeded('Frozen masked profile inputs exceed byte budget')
                result[str(keys[key])] = value
        return result
