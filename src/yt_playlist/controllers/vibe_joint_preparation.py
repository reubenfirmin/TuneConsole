"""Bounded joint-model capture, detached fitting and guarded publication."""
from yt_playlist.core import fao, vibe_model_files as files
from yt_playlist.core.activity import WorkBudgetExceeded


def current_values(value, revision, configuration):
    return value.get('revision') == revision and value.get('configuration') == configuration


def retry_values(previous, revision, configuration, now, reason):
    attempts = previous.get('attempts', 0) + 1 if current_values(previous, revision, configuration) else 1
    return {'revision': revision, 'configuration': configuration, 'outcome': 'deferred',
            'attempted_at': now, 'reason': reason, 'attempts': attempts,
            'retry_at': now + (60 if attempts < 3 else 900)}


class JointPreparation:
    def __init__(self, settings, predictions, *, snapshot, state, qualified, configuration, refresh,
                 fit, now, monotonic, vibes, max_rows):
        self.settings, self.predictions, self.snapshot, self.state = settings, predictions, snapshot, state
        self.qualified, self.configuration, self.refresh = qualified, configuration, refresh
        self.fit, self.now, self.monotonic, self.vibes, self.max_rows = fit, now, monotonic, vibes, max_rows

    def steps(self, memory_bytes):
        if not self.qualified():
            return False
        now = self.now()
        for vibe in self.vibes:
            previous = self.state(vibe)
            revision = self.settings.get_setting(files.REVISION) or '0'
            configuration = self.configuration()
            if current_values(previous, revision, configuration) and (
                    previous.get('outcome') != 'deferred' or now < previous.get('retry_at', 0)):
                continue
            staged = None
            try:
                with self.snapshot() as snapshot:
                    revision = snapshot.settings.get_setting(files.REVISION) or '0'
                    snapshot.settings.get_setting(files.HEAD_PREFIX + vibe)
                    originals = snapshot.predictions.joint_inputs(vibe, self.max_rows)
                    rows, input_started = [], self.monotonic()
                    for start in range(0, len(originals), 32):
                        rows.extend(snapshot.priors(originals[start:start + 32], vibe))
                        yield
                    input_seconds = self.monotonic() - input_started
                    fit_started = self.monotonic()
                    value = self.fit(rows, vibe, memory_bytes=memory_bytes)
                    generation, staged = snapshot.predictions.stage_joint_model(value) if value else ('', None)
                    outcome = {'revision': revision, 'configuration': configuration, 'attempted_at': now,
                        'outcome': 'ready' if value else 'empty', 'wall_seconds': self.now() - now,
                        'input_seconds': input_seconds, 'fit_seconds': self.monotonic() - fit_started,
                        'recordings': len(rows),
                        'accounted_memory_bytes': value['diagnostics']['accounted_memory_bytes'] if value else 0}
                    refresh = snapshot.refresh(vibe)
                    return snapshot.publish(lambda: self.predictions.publish_joint_model(vibe, generation, outcome,
                        files.measurement_ids(value) if value else [], staged, refresh))
            except (WorkBudgetExceeded, fao.FileBudgetExceeded, fao.FileChanged) as exc:
                outcome = retry_values(previous, revision, configuration, now, str(exc))
                latest_revision = self.settings.get_setting(files.REVISION) or '0'
                refresh = not current_values(previous, latest_revision, self.configuration()) and self.refresh(vibe)
                self.predictions.defer_joint_model(vibe, outcome, refresh)
                return True
            finally:
                if staged:
                    staged.close()
        return False
