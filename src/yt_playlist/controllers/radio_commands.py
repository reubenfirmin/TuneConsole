"""Radio admission and complete launch/tuning commands, independent of HTTP."""
from contextlib import contextmanager

from yt_playlist.core.activity import cancellable_work, WorkClosing
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec import access, radio_tuning


class RadioCommands:
    def __init__(self, radio, player, settings, *, completed, require, logger):
        self.radio, self.player, self.settings = radio, player, settings
        self.completed, self.require, self.logger = completed, require, logger

    @contextmanager
    def admit_start(self, data):
        # Admission runs at request arrival, before the command enters a worker.
        # It preserves stop/start ordering even when worker scheduling reverses.
        with self.radio.launch_request(data.get('launch_id')) as launch:
            launch.trace = bool(launch.launch_id and type(data.get('clicked_at_ms')) is int)
            intent = (self.player.begin_playback_intent()
                      if (not launch.replay or launch.recover_navigation) and not launch.cancelled.is_set() else None)
            yield intent, launch

    def start(self, data, intent, launch):
        if launch.recover_seeded:
            return self.radio.recover_seeded_start(launch, intent)
        if launch.replay:
            return self.radio.replay_start(launch)
        cancelled = launch.cancelled
        try:
            with cancellable_work(cancelled.is_set):
                if cancelled.is_set():
                    raise WorkClosing('Radio launch stopped')
                with work_span('radio: launch access', announce=True):
                    if not self.radio.active and not self._starter(data):
                        self.require('radio', load_radio=self.radio.bootstrap)
        except WorkClosing:
            return {'ok': False, 'reason': 'Stopped during start.'}
        return self.radio.start(data.get('video_id'), data.get('local_date'), data.get('menu_id'),
            playlist_id=data.get('playlist_id'), intent=intent, launch=launch)

    def _starter(self, data):
        return (data.get('playlist_id') is None and data.get('video_id') and data.get('menu_id')
                and not self.completed() and self.settings.get_setting('onboard_listening_format') == 'radio'
                and self.radio.starter_seed(data['menu_id'], data['video_id']))

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
