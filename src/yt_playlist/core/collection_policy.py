"""Album matching, listening thresholds and collection snapshot admission."""

RETURN_DAYS = 2
ALIAS_RETRY_SECONDS = 900
ALIAS_RETENTION_SECONDS = 30 * 86400
RETAINED_TRACKLIST_REVISIONS = 2


def tracklist_rows(keys, entries, recordings):
    if entries is not None:
        return [dict(entry, position=index) for index, entry in enumerate(entries)]
    rows = []
    for key in keys:
        recording = recordings.get(key, {'recording_id': None, 'entries': []})
        if len(recording['entries']) == 1:
            row = {'entry_id': recording['entries'][0]}
        else:
            row = {'recording_id': recording['recording_id'], 'raw_identity_key': key}
        rows.append(row)
    return rows


def retired_tracklists(rows):
    retained, retired = {}, []
    for row in rows:
        retained[row['status']] = retained.get(row['status'], 0) + 1
        if retained[row['status']] > RETAINED_TRACKLIST_REVISIONS:
            retired.append(row['id'])
    return retired


def album_title(value):
    return ' '.join(str(value or '').split()).casefold()


def accepted_snapshots(snapshots, accounts, expected_revisions):
    return [identity for identity in snapshots if identity in accounts and accounts[identity]['active'] and
        (expected_revisions is None or identity in expected_revisions and
         expected_revisions[identity] == accounts[identity]['revision'])]
