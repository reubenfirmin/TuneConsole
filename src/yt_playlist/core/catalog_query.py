"""Coherent playback representatives; filter caller eligibility before choosing an upload."""


def playable_tracks_sql(eligible_sql=None):
    """One complete row per recording, using its first eligible available library entry.

    Scoped callers supply a SELECT of eligible tracks (with parameters bound by
    their caller). Never select a global representative and then filter away a
    recording whose other upload was eligible for this account, artist or release.
    """
    if eligible_sql is None:
        return 'SELECT * FROM playable_library_tracks'
    return (f'WITH eligible AS ({eligible_sql}) SELECT e.* FROM eligible e '
            'WHERE e.id IN (SELECT MIN(id) FROM eligible '
            "WHERE NULLIF(trim(video_id),'') IS NOT NULL AND COALESCE(available,1)<>0 "
            'GROUP BY recording_id)')
