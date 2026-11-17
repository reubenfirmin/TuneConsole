"""Compose the documented inventory for the architecture graph, without I/O."""
from dataclasses import asdict

from yt_playlist.model_catalog import audio, features, services, taste
from yt_playlist.model_catalog.contracts import PERIODS


def graph():
    definitions = (*taste.MODELS, *audio.MODELS, *services.MODELS, *features.FEATURES)
    edges = (*taste.EDGES, *audio.EDGES, *services.EDGES, *features.EDGES)
    nodes = {node.id: asdict(node) for node in definitions}
    if len(nodes) != len(definitions):
        raise ValueError("Duplicate model inventory identity")
    for node in nodes.values():
        if node['area'] == 'feature' and node['app_area'] not in features.FEATURE_AREAS:
            raise ValueError(f"Missing application area for feature: {node['id']}")
        node.update(incoming=[], outgoing=[])
    seen = set()
    for edge in edges:
        pair = edge.source, edge.target
        if pair in seen or edge.source not in nodes or edge.target not in nodes:
            raise ValueError(f"Invalid model inventory connection: {pair}")
        seen.add(pair)
        for owner, other, direction in ((edge.target, edge.source, "incoming"),
                                         (edge.source, edge.target, "outgoing")):
            nodes[owner][direction].append({
                "id": other, "title": nodes[other]["title"],
                "detail": edge.detail, "condition": edge.condition,
            })
    groups = list(dict.fromkeys(node.group for node in definitions if node.area == "model"))
    return {"nodes": list(nodes.values()), "edges": [asdict(edge) for edge in edges],
            "groups": groups,
            "feature_areas": features.FEATURE_AREAS,
            "periods": [{"id": key, "title": title, "description": description}
                        for key, title, description in PERIODS]}
