"""Read detached metadata-source references for audio inference."""
import json
from contextlib import ExitStack, contextmanager

from . import metadata_delivery_files, metadata_native, metadata_runtime_files, policy_documents
from .activity import WorkBudgetExceeded


class AudioMetadataFiles:
    def __init__(self, runtime_root, delivery_root, prepared_root=None):
        self.runtime_root, self.delivery_root = runtime_root, delivery_root
        self.prepared_root = prepared_root

    @contextmanager
    def retained(self):
        with ExitStack() as guards:
            for root in (self.runtime_root, self.delivery_root, self.prepared_root):
                if root is not None:
                    guards.enter_context(policy_documents.retained_reader(root))
            yield

    def source_values(self, reference, *, max_bytes=256 * 1024):
        if 'raw' in reference:
            return json.loads(reference['raw'])
        if 'revision' in reference:
            if reference['revision'] is None:
                return {}
            return metadata_runtime_files.read_at(self.runtime_root, 'native', (reference['key'],),
                                                  reference['revision'], max_bytes=max_bytes)['observations']
        if 'prepared' in reference:
            from .preparation import metadata_destinations
            rows = [metadata_destinations.original(row) for row in reference['prepared']]
            return {row['field']: metadata_native._profile_value({**json.loads(row['provenance_json']),
                'field': row['field'], 'value': json.loads(row['value_json'])}) for row in rows}
        rows = reference['observations']
        if sum(row['raw_byte_size'] for row in rows) > max_bytes:
            raise WorkBudgetExceeded('Source profile input exceeds byte budget')
        bodies = {}
        for row in rows:
            identity = row['delivery_id']
            if identity not in bodies:
                bodies[identity] = (metadata_delivery_files.read(row['raw_filename'], row['raw_digest'],
                    row['raw_byte_size'], row['raw_offset']) if row['raw_filename'] is not None else None)
        return metadata_native.decode_source(rows, bodies)
