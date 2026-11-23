"""Bind Vibe prediction and recording preparation to their data sources."""
import logging
from contextlib import contextmanager
from types import SimpleNamespace

from yt_playlist.controllers.vibe_prediction import configured

log = logging.getLogger(__name__)


def recording_preparation(reader):
    from yt_playlist.controllers.vibe_recording_preparation import RecordingPreparation
    from yt_playlist.rec import acousticbrainz_vibes, musical_preparation
    from yt_playlist.rec import vibe_recording_rules as vibe_preparation, vibe_joint_state as joint

    def empty(vibe):
        state = joint.state(reader, vibe)
        return (state.get('outcome') == 'empty' and state.get('policy') == vibe_preparation.joint_policy()
                and state.get('configuration') == joint.config_identity())

    return RecordingPreparation(reader.vibe_predictions, reader.musical_profiles, reader.genres,
        prediction=lambda: prediction().capture(), version=vibe_preparation.version, qualified=joint.qualified,
        masked=lambda rows: musical_preparation.masked_recordings(
            rows, inputs=musical_preparation.load_masked_inputs(reader, rows)),
        posterior=lambda vibe: joint.posterior(reader, vibe), current_empty=empty,
        acousticbrainz_model=lambda: acousticbrainz_vibes.coefficients(
            acousticbrainz_vibes.saved_model(reader.vibe_predictions.acousticbrainz_model())))


def joint_preparation(store):
    from yt_playlist.controllers.vibe_joint_preparation import JointPreparation
    from yt_playlist.rec import vibe_joint_state as preparation, vibe_joint_model as model
    from yt_playlist.rec.vibe_recording_rules import joint_policy

    @contextmanager
    def snapshot(vibe, configuration):
        with store.read_snapshot() as captured:
            reader = captured.store
            head, previous = reader.vibe_predictions.joint_state(vibe)
            policy = joint_policy()

            def publish(target):
                reason = 'database-readmission'

                def guarded():
                    nonlocal reason
                    if joint_policy() != policy or preparation.config_identity() != configuration:
                        reason = 'changed-vibe-policy'
                    elif store.vibe_predictions.joint_state(vibe) != (head, previous):
                        reason = 'changed-vibe-head'
                    else:
                        target()
                        reason = None

                # The generation records the exact captured evidence. Later source
                # writes schedule a new fit; head replacement still has one owner.
                captured.publish_ordered(guarded, validate_inputs=False)
                if reason:
                    store.maintenance_diagnostics.record('deferred', reasons=(reason,), phase='vibe_joint_fit')
                    log.debug('Vibe fit publication rejected channel=%s reason=%s', vibe, reason)
                return reason is None

            yield SimpleNamespace(predictions=reader.vibe_predictions,
                previous=previous, head=head, policy=policy, publish=publish,
                priors=lambda rows, vibe: preparation.prior_inputs(reader, rows, vibe),
            )

    return JointPreparation(store.vibe_predictions, snapshot=snapshot,
        next_fit=lambda waiting: preparation.next_fit(store, waiting=waiting), configuration=preparation.config_identity,
        fit=model.fit, now=preparation.time.time, monotonic=preparation.time.monotonic,
        max_rows=model.MAX_ROWS)


def prediction():
    return configured()
