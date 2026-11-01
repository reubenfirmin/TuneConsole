"""Join the author's Emotify+ annotations to audio features already acquired."""
from collections import Counter, defaultdict
import csv
import json
from pathlib import PurePosixPath

import numpy as np

from .external_experiment import DATA, OUT, digest, save, stamp
from .external_secondary import identity

SHA256 = '066060187c2ee5116bb97c7b6be989a277519fe75b634ef7901f576e3e778e87'
SOURCE_URL = ('https://data.mendeley.com/public-files/datasets/wbk8zr9bd2/files/'
              '4d114c5b-b5a8-49c4-94ce-655fd6ff721a/file_downloaded')
TARGETS = ('relaxing', 'energizing', 'amusing', 'dreamy', 'happy',
           'neutral', 'sad', 'anxious', 'joyful', 'annoying')
GENRES = {'classic': 'classical', 'electric': 'electronic', 'pop': 'pop', 'rock': 'rock'}


def prepare(base='emotify-artist', output='emotify-plus-strict'):
    annotations = DATA / 'emotify-plus/musicemotions_dataset_v2.csv'
    if digest(annotations) != SHA256:
        raise ValueError('Emotify+ annotations differ from the pinned Mendeley release')
    source_path = OUT / f'{base}.features.json'
    cached = json.loads(source_path.read_text())
    features = {PurePosixPath(row['audio']).parent.name + '/' + PurePosixPath(row['audio']).name: row
                for row in cached['rows']}
    if len(features) != len(cached['rows']):
        raise ValueError('Ambiguous Emotify genre/filename identities')
    with annotations.open(encoding='utf-8-sig', newline='') as stream:
        supplied = list(csv.DictReader(stream))
    by_track = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    titles, song_ids, mismatches = defaultdict(set), {}, []
    genre_discrepancies = defaultdict(list)
    for row in supplied:
        filename = PurePosixPath(row['fileName'])
        if len(filename.parts) != 2 or filename.is_absolute() or '..' in filename.parts:
            raise ValueError('Invalid Emotify+ audio identity')
        name = str(filename)
        if name not in features:
            raise ValueError('Emotify+ audio join failed: ' + name)
        if filename.parent.name.casefold() != GENRES.get(row['Genre'].casefold()):
            genre_discrepancies[name].append(row['Genre'])
        if row['Feeling'] not in TARGETS or not 0 <= int(row['Rating']) <= 5:
            raise ValueError('Unexpected native emotion/intensity value')
        if name in song_ids and song_ids[name] != row['songID']:
            raise ValueError('Emotify+ filename has multiple song IDs')
        song_ids[name] = row['songID']
        titles[name].add(row['SongName'].strip())
        by_track[name][row['UserID']][row['Feeling']].append(int(row['Rating']))
    if len(set(song_ids.values())) != len(song_ids):
        raise ValueError('An Emotify+ song ID refers to different files')
    rows = []
    for filename, listeners in sorted(by_track.items()):
        original = features[filename]
        if any(identity(title) != identity(original.get('title', '')) for title in titles[filename]):
            mismatches.append({'filename': filename, 'annotation_titles': sorted(titles[filename]),
                               'supplied_audio_title': original.get('title', '')})
            continue
        if filename in genre_discrepancies:
            continue
        selected = np.array([[int(name in feelings) for name in TARGETS]
                             for feelings in listeners.values()], dtype=float)
        intensities, supports = {}, {}
        for name in TARGETS:
            observed = [float(np.mean(feelings[name])) for feelings in listeners.values() if name in feelings]
            intensities[name] = float(np.mean(observed)) if observed else None
            supports[name] = len(observed)
        rows.append({**original, 'target_names': list(TARGETS), 'targets': selected.mean(axis=0).tolist(),
                     'rater_count': len(listeners), 'native_scale': [0, 1],
                     'annotation_unit': 'excerpt_perceived_emotion_selection_fraction',
                     'annotation_file': filename, 'annotation_song_id': song_ids[filename],
                     'conditional_intensity_means_0_to_5': intensities,
                     'emotion_listener_counts': supports})
    policy = ('Exclude a complete track if any supplied annotation title disagrees with its audio tag after '
              'Unicode/case/punctuation normalization, or its genre disagrees after explicit CLASSIC/ELECTRIC aliases. '
              'Do not infer corrections for typos, alternate titles or conflicting release rows. '
              'For each retained genre/filename, count each identified listener once per emotion and divide '
              'by distinct listeners for that track. Repeated emotion rows do not add votes. Targets measure '
              'selection frequency, not proof that an unselected emotion is absent. Conditional 0–5 intensity '
              'judgments remain separate; missing emotion intensities are not fabricated as zero.')
    audit = {'created_at': stamp(), 'source_url': SOURCE_URL, 'annotation_sha256': SHA256,
        'source_features_sha256': digest(source_path), 'script_sha256': digest(__file__),
        'annotation_rows': len(supplied), 'filename_joins': len(by_track), 'strict_audio_joins': len(rows),
        'native_song_ids': len(set(song_ids.values())),
        'distinct_listener_ids': len({row['UserID'] for row in supplied}),
        'listener_track_pairs': sum(len(v) for v in by_track.values()),
        'repeated_listener_track_emotion_rows': sum(len(values) - 1 for listeners in by_track.values()
            for feelings in listeners.values() for values in feelings.values()),
        'unannotated_audio': sorted(set(features) - set(by_track)),
        'title_discrepancies': mismatches,
        'genre_discrepancies': {key: dict(Counter(values)) for key, values in genre_discrepancies.items()},
        'excluded_audio': sorted(set(by_track) - {row['annotation_file'] for row in rows}),
        'listener_count_distribution': dict(sorted(Counter(row['rater_count'] for row in rows).items())),
        'policy': policy, 'limitations': [
            'The release is additional supervision on original Emotify audio; preserve shared artist/recording splits.',
            'The paper reports 181 volunteers; this CSV has the distinct listener count recorded above.',
            'Ratings include zero although the paper describes a 1–5 intensity scale; preserve zero values.',
            'Listener/song events lack unique event IDs or timestamps; selection uses listener-level deduplication.',
            'Strict exclusions include benign shortened titles and typos; results concern the retained subset only.',
            'The author release contains conflicting title/genre fields; filename alone is insufficient verification.',
            'Participant cultural/gender imbalance limits population generalization.']}
    save(OUT / f'{output}.join-audit.json', audit)
    save(OUT / f'{output}.features.json', {**cached, 'rows': rows,
         'annotation_provenance': {'sha256': SHA256, 'policy': policy,
                                   'source_features_sha256': digest(source_path)}})
    print(json.dumps({'emotify_plus': {k: audit[k] for k in ('annotation_rows', 'filename_joins', 'strict_audio_joins',
        'distinct_listener_ids', 'listener_track_pairs', 'repeated_listener_track_emotion_rows')},
        'title_discrepancies': len(mismatches)}), flush=True)
    return audit
