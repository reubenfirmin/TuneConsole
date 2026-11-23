"""Bounded joint-model capture, detached fitting and guarded publication."""
import logging

from yt_playlist.core import fao, vibe_model_files as files
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping

log = logging.getLogger(__name__)
DISPATCH_VERSION = 1


def current_values(value, revision, configuration):
    return value.get('revision') == revision and value.get('configuration') == configuration


def retry_values(previous, revision, configuration, now, reason):
    attempts = previous.get('attempts', 0) + 1 if previous.get('configuration') == configuration else 1
    return {'revision': revision, 'configuration': configuration, 'outcome': 'deferred',
            'attempted_at': now, 'reason': reason, 'attempts': attempts,
            'retry_at': now + (60 if attempts < 3 else 900)}


def sampled_artists(value, previous=None):
    earlier = {row['recording']: row for row in previous['inputs']} if previous else {}
    latest = {}
    for row in value['inputs']:
        check_stopping()
        if row['source'].startswith('audio_measurement/') and row != earlier.get(row['recording']):
            for artist in row['artists']:
                latest[artist] = max(row['measurement'], latest.get(artist, 0))
    return sorted(latest, key=lambda artist: (-latest[artist], artist))


def installed_model(snapshot, vibe):
    try:
        value = snapshot.predictions.joint_model(snapshot.head)
    except fao.CorruptFile:
        return None
    return value if value['vibe'] == vibe else None


class JointPreparation:
    def __init__(self, predictions, *, snapshot, next_fit, configuration, fit, now, monotonic, max_rows):
        self.predictions, self.snapshot, self.next_fit = predictions, snapshot, next_fit
        self.configuration, self.fit = configuration, fit
        self.now, self.monotonic, self.max_rows = now, monotonic, max_rows

    def steps(self, memory_bytes, *, waiting=False):
        if not (vibe := self.next_fit(waiting)):
            return False
        now = self.now()
        configuration = self.configuration()
        staged = None
        input_started = self.monotonic()
        with self.snapshot(vibe, configuration) as snapshot:
            revision = snapshot.predictions.joint_revision()
            try:
                previous = snapshot.previous
                compatible = previous.get('policy') == snapshot.policy and previous.get('configuration') == configuration
                dispatched = compatible and previous.get('dispatch_version') == DISPATCH_VERSION
                if (snapshot.head and compatible and not dispatched and previous.get('outcome') == 'ready'
                        and current_values(previous, revision, configuration)):
                    if installed := installed_model(snapshot, vibe):
                        artists = sampled_artists(installed)
                        outcome = {**previous, 'dispatch_version': DISPATCH_VERSION}
                        return snapshot.publish(lambda: self.predictions.publish_joint_model(
                            vibe, snapshot.head, outcome, [], None, False, artists=artists))
                originals = snapshot.predictions.joint_inputs(vibe, self.max_rows)
                rows = []
                for start in range(0, len(originals), 32):
                    rows.extend(snapshot.priors(originals[start:start + 32], vibe))
                    yield
                fit_started = self.monotonic()
                value = self.fit(rows, vibe, memory_bytes=memory_bytes)
                stage_started = self.monotonic()
                generation, staged = snapshot.predictions.stage_joint_model(value) if value else ('', None)
                changed = generation != (snapshot.head or '')
                artists = ()
                if value and (changed or not dispatched):
                    earlier = installed_model(snapshot, vibe) if dispatched and snapshot.head else None
                    artists = sampled_artists(value, earlier)
                    del earlier
                publish_started = self.monotonic()
                outcome = {'revision': revision, 'configuration': configuration, 'policy': snapshot.policy,
                    'dispatch_version': DISPATCH_VERSION,
                    'attempted_at': now, 'outcome': 'ready' if value else 'empty',
                    'wall_seconds': self.now() - now, 'input_seconds': fit_started - input_started,
                    'fit_seconds': stage_started - fit_started, 'stage_seconds': publish_started - stage_started,
                    'recordings': len(rows),
                    'accounted_memory_bytes': value['diagnostics']['accounted_memory_bytes'] if value else 0}
                published = snapshot.publish(lambda: self.predictions.publish_joint_model(vibe, generation, outcome,
                    files.measurement_ids(value) if value else [], staged, changed, artists=artists))
                log.debug('Vibe fit channel=%s published=%s publication_seconds=%.6f %s',
                          vibe, published, self.monotonic() - publish_started, outcome)
                return published
            except (WorkBudgetExceeded, fao.FileBudgetExceeded, fao.FileChanged) as exc:
                outcome = retry_values(snapshot.previous, revision, configuration, now, str(exc))
                outcome['policy'] = snapshot.policy
                if dispatched:
                    outcome['dispatch_version'] = DISPATCH_VERSION
                log.debug('Vibe fit deferred channel=%s %s', vibe, outcome)
                return snapshot.publish(lambda: self.predictions.defer_joint_model(vibe, outcome, False))
            finally:
                if staged:
                    staged.close()
