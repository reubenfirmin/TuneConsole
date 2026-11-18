"""Grouping budgets and compatibility entry point for the preparation command."""
from dataclasses import dataclass
import json

from yt_playlist.controllers.group_preparation import BOOKKEEPING, CHECKPOINT, PROGRESS, report as report_progress
from yt_playlist.rec import group_discovery



def status(store):
    return json.loads(store.settings.get_setting(PROGRESS) or '{}')


def report(store, phase, now, *, reason=None):
    return report_progress(store.settings, phase, now, reason=reason)


@dataclass(frozen=True)
class Budget:
    seconds: float = 10
    interval: float = 60
    max_rows: int = 16384
    input_bytes: int = 8 * 1024 * 1024


def Preparation(store, *, policy=group_discovery.Policy(), budget=Budget()):
    from yt_playlist.library.group_composition import preparation
    return preparation(store, policy=policy, budget=budget)
