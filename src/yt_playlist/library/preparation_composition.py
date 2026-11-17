"""Bounded source commands for progressive preparation's fair turn scheduler."""
from types import SimpleNamespace
from yt_playlist.rec import musical_preparation, vibe_joint_preparation, vibe_preparation


def source_commands(preparation):
    store, budget = preparation.store, preparation.budget
    return SimpleNamespace(capturing=lambda: preparation._capturing is not None,
        advance=preparation._advance_capture, repair=preparation.repair,
        joint_active=lambda: vibe_joint_preparation.active(store),
        joint_scoring=lambda: vibe_joint_preparation.scoring(store),
        vibe=lambda: vibe_preparation.step(store, preparation.batch,
            seconds=budget.fit_seconds, memory_bytes=budget.memory_bytes, joint_step=preparation._joint_step),
        fanout=lambda: store.vibe_predictions.fanout(preparation.batch),
        pending=lambda: store.prepared_content.pending(preparation.batch),
        start=preparation._start_capture,
        musical=lambda now: musical_preparation.source_step(store, now, batch=preparation.batch,
            seconds=budget.capture_seconds),
        musical_pending=lambda: store.musical_profiles.pending())
