"""Retain bounded native tags; descriptive filtering belongs to profile resolution."""
from yt_playlist.core.metadata_observations import MetadataObservation


def retain(observations, *, scope, entity_id, tags, source_path, match_evidence):
    if observations is None or not entity_id:
        return
    values = []
    for tag in tags[:64]:
        if isinstance(tag, str):
            tag = {'name': tag}
        if isinstance(tag, dict) and isinstance(tag.get('name'), str) and tag['name'].strip():
            values.append({key: tag[key] for key in ('name', 'count', 'kind') if key in tag})
    observations.append(MetadataObservation(
        field='tags', value=values, scope=scope, entity_id=str(entity_id), kind='reported',
        unit='provider_tag_count_or_order', source_path=source_path, match_evidence=match_evidence))
