"""Cleanup acquisition, detached analysis and preference commands."""
from contextlib import nullcontext


def prepare_cleanup(store):
    """Compatibility composition for background consumers and snapshot checks."""
    from yt_playlist.library import cleanup_analysis as analysis
    context = nullcontext(store) if getattr(store, '_read_only', False) else store.read_snapshot()
    with context as reader:
        reader = getattr(reader, 'store', reader)
        memberships = reader.playlists.cleanup_memberships()
        preferences = reader.overlaps.cleanup_preferences()
    return analysis.CleanupAnalysis(memberships, preferences)


class Cleanup:
    def __init__(self, preferences, accounts, *, capture, publish, paused, now):
        self.preferences, self.accounts = preferences, accounts
        self.capture, self.publish, self.paused, self.now = capture, publish, paused, now

    def page(self):
        from yt_playlist.library import cleanup_analysis as analysis
        memberships, preferences = self.capture()
        result = analysis.analyze_cleanup(analysis.CleanupAnalysis(memberships, preferences))
        if not self.paused():
            self.publish(result.summary().as_payload(), self.now())
        return result, self.accounts.get_accounts()

    def change(self, operation, *values):
        method = getattr(self.preferences, operation)
        timed = {'ignore_overlap_playlist', 'keep_overlap_pair', 'suppress_overlap',
                 'suppress_overlaps', 'ignore_cleanup', 'ignore_merge'}
        return method(*values, self.now()) if operation in timed else method(*values)

    def mute_others(self, a, b):
        at = self.now()
        self.preferences.keep_overlap_pair(a, b, at)
        self.preferences.ignore_overlap_playlist(a, at)
        self.preferences.ignore_overlap_playlist(b, at)

    def ignore_merge(self, members):
        from yt_playlist.library import cleanup_analysis as analysis
        self.change('ignore_merge', analysis.merge_signature(members), members)
