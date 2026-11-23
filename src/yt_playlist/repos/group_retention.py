"""Retire inactive generations once their direct references are gone."""
from yt_playlist.core.group_evidence import HOME_CONTEXTS, HOME_VERSIONS
from yt_playlist.core.group_preparation_state import ROOTS
from .base import Repo
from .group_publication import _write, _CLEANUP_OWNER


class GroupRetentionRepo(Repo):
    def step(self):
        with _write(self) as conn:
            removed = conn.execute(f'''
                UPDATE group_generations
                SET is_published=0,published_at=NULL,build_owner=?,build_expires_at=0
                WHERE id=(
                    SELECT g.id FROM group_generations g
                    WHERE g.is_active=0 AND g.is_published=1
                      AND EXISTS(SELECT 1 FROM settings WHERE key=? AND value='1')
                      AND g.worker_checkpoint_at IS NULL
                      AND NOT EXISTS(SELECT 1 FROM group_generations WHERE build_parent_id=g.id)
                      AND NOT EXISTS(SELECT 1 FROM recommendation_generations WHERE group_generation_id=g.id)
                      AND g.evidence_holds=0
                      AND NOT EXISTS(SELECT 1 FROM group_versions v WHERE v.generation_id=g.id AND v.evidence_holds>0)
                      AND NOT EXISTS(SELECT 1 FROM {HOME_CONTEXTS} AND context.value->>7=g.id)
                      AND NOT EXISTS(SELECT 1 FROM group_versions v WHERE v.generation_id=g.id
                                     AND v.id IN (SELECT reference.value->>2 FROM {HOME_VERSIONS}))
                      AND NOT EXISTS(SELECT 1 FROM group_versions origin
                                     JOIN group_versions other ON other.external_version=origin.external_version
                                     JOIN group_generations retained ON retained.id=other.generation_id
                                     WHERE origin.generation_id=g.id AND origin.is_origin=1
                                       AND other.generation_id<>g.id
                                       AND (retained.is_published=1 OR retained.build_owner<>'cleanup'))
                    ORDER BY g.id LIMIT 1
                ) RETURNING id
            ''', (_CLEANUP_OWNER, ROOTS)).fetchone()
        return removed is not None
