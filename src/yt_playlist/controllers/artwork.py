"""One bounded artwork retry, independent of the metadata pass and transport."""
from yt_playlist.util.thumbnails import needs_art


def resolve(sink, plan, lookup, result, now):
    """Publish the selected cover, acquiring archive fallback only when requested."""
    art, release_ids, kind = plan
    if kind == 'retained':
        sink.record_artwork([], result('found', art), now())
        return None
    outcome = result('found', art) if art else lookup(release_ids) if release_ids else result('absent')
    sink.set_art(outcome.url, source='deezer' if art else 'coverart')
    sink.record_artwork(release_ids, outcome, now())
    return outcome


class Artwork:
    def __init__(self, state, tracks, lookup, result, *, now):
        self.state, self.tracks = state, tracks
        self.lookup, self.result, self.now = lookup, result, now

    def retry_due(self, *, should_stop=lambda: False, on_progress=lambda event: None):
        completed = 0
        for job in self.state.due_artwork_retries(self.now(), limit=1):
            if should_stop():
                break
            track = self.tracks.catalog_subject(job['entry_id'])
            if not track:
                continue
            # A retained archive image can be refreshed. A later user or other
            # provider image completes the old request without replacing it.
            if needs_art(track) or self.tracks.artwork_source(track['id']) == 'coverart':
                outcome = self.lookup(job['release_ids'])
                if outcome.url:
                    self.tracks.set_track_art(track['id'], outcome.url, source='coverart')
            else:
                outcome = self.result('found', track.get('thumbnail'))
            self.state.record_artwork_result(track['id'], job['release_ids'], outcome, self.now())
            if outcome.status == 'retry':
                on_progress({'type': 'info', 'warning': True,
                             'text': f"Artwork retry for {track['title']} failed: {outcome.error}"})
            else:
                completed += 1
        return completed
