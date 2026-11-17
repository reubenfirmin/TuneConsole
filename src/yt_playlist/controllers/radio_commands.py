"""Radio admission and complete launch/tuning commands, independent of HTTP."""
from contextlib import contextmanager

from yt_playlist.core.activity import cancellable_work, WorkClosing
from yt_playlist.rec import access, radio_tuning


def starter_launch(data, bootstrap, *, setup_completed, listening_format):
    return (not setup_completed and listening_format == 'radio' and data.get('playlist_id') is None
            and data.get('menu_id') == bootstrap.get('seedMenu')
            and any(t.get('video_id') == data.get('video_id') for t in bootstrap['seeds']))


class RadioCommands:
    def __init__(self, radio, player, settings, *, completed, require, logger):
        self.radio, self.player, self.settings = radio, player, settings
        self.completed, self.require, self.logger = completed, require, logger

    @contextmanager
    def admit_start(self, data):
        # Admission runs at request arrival, before the command enters a worker.
        # It preserves stop/start ordering even when worker scheduling reverses.
        with self.radio.launch_request(data.get('launch_id')) as cancelled:
            intent = None if cancelled.is_set() else self.player.begin_playback_intent()
            yield intent, cancelled

    def start(self, data, intent, cancelled):
        try:
            with cancellable_work(cancelled.is_set):
                if cancelled.is_set():
                    raise WorkClosing('Radio launch stopped')
                bootstrap = self.radio.bootstrap()
                starter = starter_launch(data, bootstrap, setup_completed=self.completed(),
                                         listening_format=self.settings.get_setting('onboard_listening_format'))
                if not starter and not bootstrap['state'].get('active'):
                    self.require('radio', radio=bootstrap)
        except WorkClosing:
            return {'ok': False, 'reason': 'Stopped during start.'}
        return self.radio.start(data.get('video_id'), data.get('local_date'), data.get('menu_id'),
            playlist_id=data.get('playlist_id'), intent=intent, cancelled=cancelled)

    def admit_tuning(self, data):
        values = radio_tuning.controls(data)
        return values, self.radio.reserve_tuning()

    def tune(self, values, serial):
        try:
            if values['energy'] != .5:
                self.require('vibes')
        except access.AccessDenied:
            self.logger.info('Radio tuning refused: reason=energy_locked values=%s', values)
            return {'ok': False, 'tune_reason': 'energy_locked'}
        self.logger.info('Radio tuning admitted: values=%s', values)
        return self.radio.tune(values, serial=serial)

    def admit_select(self, video_id):
        return self.player.begin_playback_intent() if video_id else None

    def admit_resume(self):
        return self.player.begin_playback_intent()
