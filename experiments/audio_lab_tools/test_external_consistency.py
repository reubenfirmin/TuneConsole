"""Check that deduplication cannot conceal cross-partition recording leakage."""
import pytest

from experiments.audio_lab_tools import external_consistency as consistency
from experiments.audio_lab_tools.external_experiment import save


def test_duplicate_audio_in_different_partitions_fails_before_fitting(tmp_path, monkeypatch):
    monkeypatch.setattr(consistency, 'CONSISTENCY', tmp_path)
    save(tmp_path / 'pairs.json', {'pairs': [
        {'id': 'train', 'audio_sha256': 'same-audio'},
        {'id': 'holdout', 'audio_sha256': 'same-audio'}]})
    save(tmp_path / 'pair-split.json', {'training_ids': ['train'], 'holdout_ids': ['holdout']})
    monkeypatch.setattr(consistency, 'compare', lambda *args, **kwargs: pytest.fail('Training must not start'))
    with pytest.raises(ValueError, match='Duplicate audio crosses'):
        consistency.compare_deduplicated()


def test_same_partition_deduplication_preserves_fixed_holdout(tmp_path, monkeypatch):
    monkeypatch.setattr(consistency, 'CONSISTENCY', tmp_path / 'pairs')
    monkeypatch.setattr(consistency, 'OUT', tmp_path)
    save(tmp_path / 'pairs/pairs.json', {'pairs': [
        {'id': 'train', 'audio_sha256': 'one'}, {'id': 'duplicate', 'audio_sha256': 'one'},
        {'id': 'holdout', 'audio_sha256': 'two'}]})
    save(tmp_path / 'pairs/pair-split.json', {'training_ids': ['train', 'duplicate'], 'holdout_ids': ['holdout']})
    save(tmp_path / 'pmemo.features.json', {'rows': [
        {'id': 'train', 'artist': 'Artist A', 'title': 'One'},
        {'id': 'holdout', 'artist': 'Artist B', 'title': 'Two'}]})
    calls = []
    monkeypatch.setattr(consistency, 'compare', lambda dataset, train, heldout, **kwargs:
                        calls.append((dataset, [row['id'] for row in train], [row['id'] for row in heldout])))
    consistency.compare_deduplicated(identity_exclusions=[{'artist': 'Artist C', 'title': 'Three'}])
    assert len(calls) == 3
    assert all(train == ['train'] and heldout == ['holdout'] for _, train, heldout in calls)
    with pytest.raises(ValueError, match='protected local identities'):
        consistency.compare_deduplicated(identity_exclusions=[{'artist': 'Artist A'}])
