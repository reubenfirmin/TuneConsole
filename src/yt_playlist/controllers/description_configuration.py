"""Paid description preferences and the publication work their changes wake."""
from yt_playlist.rec import paid_description_policy


class DescriptionConfiguration:
    def __init__(self, settings, notes, query, *, now, refresh=lambda: None):
        self.settings, self.notes, self.query = settings, notes, query
        self.now, self.refresh = now, refresh

    def save(self, **change):
        values = paid_description_policy.configuration_change(**change)
        self.settings.set_values(values)
        if not change.get('disconnect'):
            self.notes.wake(self.now())
            self.refresh()
        return None if change.get('key_only') else self.query(self.now())
