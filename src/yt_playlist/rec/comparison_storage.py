"""Lossless bounded storage for repeated tracks in paired serving projections."""
import base64
import json
import zlib

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos.policy_storage import references

LEGACY_CODEC = 'json-zlib-v1'
CODEC = 'json-shared-zlib-v2'
MAX_EXPANDED_BYTES = 64_000_000
MAX_NODES = 500_000
MAX_DEPTH = 128


class _UsePlainJSON(ValueError):
    pass


def _pack(data):
    """Intern exact JSON values, including musical profiles repeated in distant pools.

    Containers store earlier node indexes; scalar nodes retain their JSON types.
    Structural keys compare complete values, so neither hashes nor recording IDs
    can accidentally merge different scores, policies or metadata.
    """
    nodes, indexes, visits = [], {}, 0

    def visit(value, depth=0):
        nonlocal visits
        visits += 1
        if visits % 1024 == 0:
            check_cancellable_work()
        if depth >= MAX_DEPTH:
            raise _UsePlainJSON()
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise _UsePlainJSON()
            children = tuple((key, visit(child, depth + 1)) for key, child in sorted(value.items()))
            key, node = (dict, children), dict(children)
        elif isinstance(value, (list, tuple)):
            children = tuple(visit(child, depth + 1) for child in value)
            key, node = (list, children), list(children)
        else:
            # Python equates True, 1 and 1.0, and equates both signed zeros.
            # Their distinct JSON encodings must remain distinct nodes.
            key = (type(value), float(value).hex() if isinstance(value, float) else value)
            node = value
        if key in indexes:
            return indexes[key]
        if len(nodes) >= MAX_NODES:
            raise _UsePlainJSON()
        index = len(nodes)
        indexes[key] = index
        nodes.append(node)
        return index

    root = visit(data)
    return canonical({'nodes': nodes, 'root': root})


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


def encode(run_id, data):
    raw = canonical(data)
    if len(raw) > MAX_EXPANDED_BYTES:
        raise ValueError('Paired serving projection exceeds its expanded storage budget')
    compressed, codec, packed_bytes = zlib.compress(raw, level=3), LEGACY_CODEC, len(raw)
    try:
        packed = _pack(data)
        if len(packed) <= MAX_EXPANDED_BYTES:
            shared = zlib.compress(packed, level=3)
            if len(shared) < len(compressed):
                compressed, codec, packed_bytes = shared, CODEC, len(packed)
    except _UsePlainJSON:
        pass  # Existing JSON supports uncommon key/depth shapes without interning.
    # Context/policy references remain inspectable without decoding either codec.
    return json.dumps({'run_id': run_id, 'codec': codec, 'expanded_bytes': len(raw),
                       'packed_bytes': packed_bytes,
                       'context': data.get('context') or data.get('choice_context') or {},
                       'policies': [{'model_version': version} for version in sorted(set(references(data)))],
                       'data': base64.b64encode(compressed).decode('ascii')},
                      separators=(',', ':'))


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
