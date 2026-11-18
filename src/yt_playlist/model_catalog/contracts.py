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


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    detail: str
    condition: str = ""


PERIODS = (
    ("durable", "Durable", "Saved interests and slow preference changes"),
    ("recent", "Recent", "Days to weeks; evidence fades with wall-clock time"),
    ("session", "Session", "Last 24 hours, with an hours-scale decay"),
    ("now", "NOW", "Current listening window; confidence gated"),
    ("structural", "Outside the stack", "Song properties, relationships, services and selection rules"),
)
