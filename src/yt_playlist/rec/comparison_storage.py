"""Bounded reads of retained historical paired serving projections."""
import base64
import json
import zlib

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical

LEGACY_CODEC = 'json-zlib-v1'
CODEC = 'json-shared-zlib-v2'
MAX_EXPANDED_BYTES = 64_000_000
MAX_NODES = 500_000
MAX_DEPTH = 128


def _unpack(packed, expanded_bytes):
    """Validate expansion before allocating it, then produce independent containers."""
    if not isinstance(packed, dict) or set(packed) != {'nodes', 'root'}:
        raise ValueError('Invalid shared paired projection')
    nodes, root = packed['nodes'], packed['root']
    if (not isinstance(nodes, list) or not 0 < len(nodes) <= MAX_NODES
            or type(root) is not int or not 0 <= root < len(nodes)):
        raise ValueError('Invalid shared paired projection root')
    sizes, depths = [], []
    for index, node in enumerate(nodes):
        if index % 1024 == 0:
            check_cancellable_work()
        if isinstance(node, (dict, list)):
            children = node.values() if isinstance(node, dict) else node
            children = list(children)
            if any(type(child) is not int or not 0 <= child < index for child in children):
                raise ValueError('Invalid shared paired projection reference')
            size = 2 + max(0, len(children) - 1) + sum(sizes[child] for child in children)
            if isinstance(node, dict):
                size += sum(len(canonical(key)) + 1 for key in node)
            depth = 1 + max((depths[child] for child in children), default=0)
        else:
            size, depth = len(canonical(node)), 0
        if size > MAX_EXPANDED_BYTES or depth > MAX_DEPTH:
            raise ValueError('Shared paired projection exceeds its expanded storage budget')
        sizes.append(size)
        depths.append(depth)
    if sizes[root] != expanded_bytes or not isinstance(nodes[root], dict):
        raise ValueError('Invalid shared paired projection size')

    restored = 0

    def restore(index):
        nonlocal restored
        restored += 1
        if restored % 1024 == 0:
            check_cancellable_work()
        node = nodes[index]
        if isinstance(node, dict):
            return {key: restore(child) for key, child in node.items()}
        if isinstance(node, list):
            return [restore(child) for child in node]
        return node

    # Sharing is only a storage detail. Mutating one decoded menu/track must
    # never mutate another occurrence of the same original value.
    return restore(root)


def decode(payload):
    if payload.get('codec') is None:
        data = payload['data']  # Previously prepared projections remain usable.
        if not isinstance(data, dict):
            raise ValueError('Invalid paired serving projection')
        return data
    if (payload['codec'] not in (CODEC, LEGACY_CODEC) or type(payload['expanded_bytes']) is not int
            or not 0 <= payload['expanded_bytes'] <= MAX_EXPANDED_BYTES):
        raise ValueError('Unsupported paired serving projection')
    packed_bytes = payload.get('packed_bytes', payload['expanded_bytes'])
    if type(packed_bytes) is not int or not 0 <= packed_bytes <= MAX_EXPANDED_BYTES:
        raise ValueError('Invalid paired serving projection size')
    stream = zlib.decompressobj()
    raw = stream.decompress(base64.b64decode(payload['data'], validate=True),
                            packed_bytes + 1)
    if (len(raw) != packed_bytes or not stream.eof
            or stream.unused_data or stream.unconsumed_tail):
        raise ValueError('Invalid paired serving projection size')
    data = json.loads(raw)
    if payload['codec'] == CODEC:
        return _unpack(data, payload['expanded_bytes'])
    if packed_bytes != payload['expanded_bytes']:
        raise ValueError('Invalid paired serving projection size')
    if not isinstance(data, dict):
        raise ValueError('Invalid paired serving projection')
    return data
