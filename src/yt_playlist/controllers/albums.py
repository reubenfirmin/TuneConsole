"""Observe release metadata and resolve credits through their owning controllers."""
from yt_playlist.core import album_policy as policy


class Albums:
    def __init__(self, repository, artists, genres):
        self.repository, self.artists, self.genres = repository, artists, genres

    def observe(self, browse_id, *, title=None, artist=None, year=None, type=None,
                thumbnail=None, genre=None, credits=None, artist_id=None, rank=0):
        if not browse_id:
            raise ValueError('An album requires a YouTube browse ID.')
        # Resolve a delivered label before opening this repository's write unit.
        genre_key = self.genres.resolve(genre)[0] if genre not in (None, '') else None
        with self.repository.transaction():
            inputs = self.repository.observation_inputs(browse_id)
            current = inputs['album']
            values = dict(title=title, artist=artist, year=year, type=type, thumbnail=thumbnail)
            if not inputs['keyed_genres']:
                values['genre'] = genre
            prepared = policy.metadata(current, values, rank)
            if inputs['keyed_genres'] and genre not in (None, '') and (
                    not current or rank >= current['metadata_rank'] or inputs['genre'] in (None, '')):
                prepared.update(genre_key=genre_key, unresolved_genre=genre if genre_key is None else None)
            aid = self.repository.store_metadata(browse_id, current.get('id'), prepared)
        if policy.replace_credits(inputs['credits'], current.get('metadata_rank'), rank,
                                  credits=credits, artist_id=artist_id, artist=artist):
            structured = [credit for credit in (credits or []) if isinstance(credit, dict) and credit.get('name')]
            subjects = []
            if structured:
                for credit in structured:
                    provider_id = credit.get('id') or credit.get('browseId')
                    previous = self.artists.credit_subject(inputs['credits'], credit['name'])
                    if provider_id:
                        previous = self.artists.release_subject(aid, previous)
                    subject = self.artists.ensure_artist(credit['name'], provider='youtube', provider_id=provider_id, previous=previous)
                    subjects.append((subject, credit['name']))
            else:
                subjects = [(self.artists.state_id(artist_id if artist_id is not None else artist), artist or '')]
            self.repository.store_credits(aid, subjects)
        return aid

    def link(self, entry_id, browse_id=None, *, title=None, artist=None, source='youtube'):
        if not browse_id:
            return None
        aid = self.observe(browse_id, title=title, artist=artist)
        self.repository.link_entry(aid, entry_id, source)
        inputs = self.repository.linked_artist_inputs(aid)
        credits, subjects = inputs['credits'], inputs['subjects']
        if len(credits) == 1 and credits[0]['provisional'] and len(subjects) == 1:
            subject = subjects[0]
            if self.artists.has_alias(subject, credits[0]['credit']):
                previous = self.artists.release_subject(aid, credits[0]['artist_id'])
                if previous is not None:
                    subject = self.artists.reconcile_provisional(previous, subject, source='exact_release')
                self.repository.rebind_artist(aid, subject)
        return aid
