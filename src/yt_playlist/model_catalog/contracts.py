"""Detached definitions shared by the model inventory and its consumers."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    id: str
    title: str
    group: str
    kind: str
    summary: str
    output: str
    inputs: str
    maintenance: str
    temporal: str
    sources: tuple[str, ...]
    layers: tuple[str, ...] = ("structural",)
    caution: str = ""
    area: str = "model"
    feature_type: str = ""
    app_area: str = ""
    ui_location: str = ""


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    detail: str
    condition: str = ""
    feedback: bool = False


PERIODS = (
    ("now", "NOW", "Effective taste, with active listening and temporary steering"),
    ("session", "TODAY", "Current local calendar day, including earlier sessions"),
    ("recent", "RECENT", "Bounded multi-day evidence with source-specific decay"),
    ("durable", "PERMANENT", "Learned taste from sustained evidence across days and episodes"),
    ("structural", "Outside the stack", "Song properties, relationships, services and selection rules"),
)
