"""Detached input contract for https://github.com/reubenfirmin/TuneConsole/issues/296.

Acquisition supplies only confirmed memberships for exact recording keys and the
destination's exact keys. A recording may belong to several groups: these are
memberships, never a transitive union or permission to substitute an upload.
Recipes and prepared inputs carry this JSON value unchanged. #296 owns acquiring
it, applying exclusion/replenishment, final-save validation and invalidation.
"""
from typing import TypedDict


class VersionPolicy(TypedDict):
    confirmed_memberships: dict[str, list[str]]
    destination_recordings: list[str]
    allow_multiple: bool
    policy_revision: str
    membership_revision: str
