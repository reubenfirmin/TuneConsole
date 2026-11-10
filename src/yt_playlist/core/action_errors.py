"""Explicit action validation; arbitrary ValueError messages are never UI text."""


MESSAGES = {
    'playlist_missing': 'Playlist no longer exists',
    'playlist_account_mismatch': 'Playlist belongs to another account',
    'main_account_required': 'Choose a main account in Setup before continuing.',
    'account_disconnected': 'The required account is not connected',
    'song_required': 'A video ID is required',
    'rating_invalid': 'Invalid rating state',
    'rating_rejected': 'YouTube wouldn’t update this song’s rating.',
    'album_unavailable': 'YouTube Music cannot save that candidate as an album',
    'album_save_rejected': 'YouTube Music did not confirm the album save',
    'album_remove_rejected': 'YouTube Music did not confirm the album removal',
    'album_owner_required': 'Choose which account to remove this album from.',
    'album_owner_mismatch': 'This album is not saved in the selected account.',
    'account_invalid': 'Invalid account selection.',
    'song_playlist_name_required': 'Choose a song and give the playlist a name.',
    'playlist_regular_required': 'Choose a regular playlist.',
    'playlist_account_disconnected': "That playlist's account isn't connected.",
    'playlist_song_missing': 'This song is no longer in the playlist.',
    'playlist_sources_required': 'Choose at least one source playlist.',
    'playlist_name_required': "Name can't be empty",
    'playlist_tracks_required': 'Select at least one version to add.',
    'playlist_copy_account_mismatch': 'Choose a destination on the same account as the source playlists.',
    'merge_unavailable': 'Choose at least two existing playlists to merge.',
    'merge_keeper_required': 'Choose one of these playlists to keep, or update all of them.',
    'merge_system_keeper': "Can't merge into a system playlist. Pick a different playlist to keep.",
    'merge_missing_required': 'The kept playlist is missing {count} required track(s). Refusing to delete the source. Re-sync and check again.',
    'playlist_same': 'Choose different playlists. These memberships refer to the same YouTube Music playlist.',
    'playlist_kept': 'Refusing to delete the kept YouTube Music playlist through another membership.',
    'playlist_deletion_pending': 'This playlist already has a saved deletion. Recheck it in Actions.',
    'playlist_system_removal': "This is a system playlist and can't be removed.",
    'playlist_not_empty': "This playlist isn't empty anymore. Re-sync before removing it.",
    'playlist_cached_tracks': 'This playlist has cached tracks. Re-sync before removing it.',
    'fresh_account_required': 'Connect YouTube Music to find fresh songs.',
    'fresh_genres_required': 'Choose some genres to find fresh songs.',
    'fresh_preferences_changed': 'Your mix settings changed. Try finding fresh songs again.',
    'song_add_rejected': "YouTube couldn't add this song. Please try another song or playlist.",
    'album_feedback_required': 'Choose an album feedback action',
    'album_required': 'no album given',
    'album_load_unavailable': "couldn't load that album",
    'album_unplayable': 'this album has no playable tracks',
    'album_tracklist_unavailable': 'The full album track list is unavailable. Reconnect to YouTube Music and try again.',
    'album_tracklist_incomplete': 'The album track list is incomplete. Refresh the album and try again.',
    'album_unshareable': 'Some album tracks have no shareable video link. Try again when all tracks are available.',
}


class ActionInputError(ValueError):
    """A known validation/rejection outcome with fixed, human-readable wording."""

    def __init__(self, code, **values):
        self.code = code
        super().__init__(MESSAGES[code].format(**values))
