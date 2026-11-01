"""Recover artist-held-out Emotify identities from the supplied MP3 tags."""
from collections import defaultdict
import json
import subprocess

from .external_experiment import OUT, digest, save, stamp
from .external_secondary import identity


def prepare(lifetime_fd=None):
    source = OUT / 'emotify.features.json'
    cached = json.loads(source.read_text())
    rows, missing, tracks = [], [], defaultdict(list)
    for row in cached['rows']:
        if digest(row['audio']) != row['audio_sha256']:
            raise ValueError('Supplied Emotify audio changed: ' + row['id'])
        tags = json.loads(subprocess.check_output(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format_tags', '-of', 'json', row['audio']],
            text=True, timeout=15, pass_fds=() if lifetime_fd is None else (lifetime_fd,)))
        tags = {key.lower(): value.strip() for key, value in tags.get('format', {}).get('tags', {}).items()}
        artist, title = tags.get('artist', ''), tags.get('title', '')
        if not artist or not title:
            missing.append(row['id'])
        group = 'emotify:artist:' + identity(artist) if artist else row['group']
        rows.append({**row, 'artist': artist, 'title': title, 'album': tags.get('album', ''),
                     'group': group, 'identity_source': 'supplied_mp3_id3_tags'})
        if artist and title:
            tracks[identity(artist), identity(title)].append(row['id'])
    save(OUT / 'emotify-artist.features.json', {**cached, 'rows': rows,
         'identity_provenance': {'source_features_sha256': digest(source), 'script_sha256': digest(__file__),
                                 'policy': 'Artist and title from supplied MP3 tags; audio features and native judgments unchanged.'}})
    report = {'created_at': stamp(), 'rows': len(rows),
        'artists': len({identity(row['artist']) for row in rows if row['artist']}),
        'artist_groups': len({row['group'] for row in rows}), 'missing_identities': missing,
        'duplicate_artist_title_groups': [ids for ids in tracks.values() if len(ids) > 1],
        'source_features_sha256': digest(source),
        'artist_features_sha256': digest(OUT / 'emotify-artist.features.json'),
        'old_group_count': len({row['group'] for row in cached['rows']}),
        'policy': 'New alias preserves the historical recording-only results and cached features. Repeated artist/title identities stay in one artist group.',
        'limitations': ['Tags establish supplied performer credits, not complete composer/collaborator identities.',
                       'Identical title/artist tags do not prove identical masters or audio excerpts.']}
    save(OUT / 'emotify-artist.identity-audit.json', report)
    print(json.dumps({'emotify_identity_audit': report}), flush=True)
    return report


def row_metadata():
    rows = json.loads((OUT / 'emotify-artist.features.json').read_text())['rows']
    return {row['id']: {key: row[key] for key in ('artist', 'title', 'group')} for row in rows}
