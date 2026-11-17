"""Match detached queue facts without treating navigation as playback."""


def album_context(pending, message, now, *, account_id, horizon):
    if not pending or not 0 <= now - pending['at'] <= horizon:
        return None
    if pending.get('account_id') is not None and account_id is not None and pending['account_id'] != account_id:
        return None
    if pending.get('videos') and message.get('videoId') not in pending['videos']:
        return None
    if message.get('playlist') == pending['playlist'] or message.get('albumBrowseId') == pending['browse']:
        return pending
    return None
