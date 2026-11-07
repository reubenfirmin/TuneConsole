"""Regression checks for source-component closure and bounded repair lineage."""
import numpy as np
import pytest

from experiments.audio_lab_tools.evaluation.transfer_identity import exclusion
from experiments.audio_lab_tools.studies import mapped_vibe_round


def test_collaboration_chain_excludes_every_connected_source_member():
    source = [{'id': 'a', 'artist': 'Coldplay+The Chainsmokers'},
              {'id': 'b', 'artist': 'The Chainsmokers+Halsey'},
              {'id': 'c', 'artist': 'Halsey'},
              {'id': 'd', 'artist': 'Unrelated'}]
    target = [{'id': 'heldout', 'artist': 'Coldplay'}]
    retained, audit = exclusion(source, target)
    assert retained.tolist() == [False, False, False, True]
    assert audit['direct_excluded_ids'] == ['a']
    assert audit['additional_excluded_ids'] == ['b', 'c']
    assert audit['anchored_components'][0]['anchors'][0]['matches'] == [('artist', 'coldplay')]
    np.testing.assert_array_equal(mapped_vibe_round.disjoint(source, target), retained)
    # Row order must not change the retained identity set.
    reversed_mask, _ = exclusion(source[::-1], target)
    assert [r['id'] for r, keep in zip(source[::-1], reversed_mask) if keep] == ['d']


@pytest.mark.parametrize('field', ['id', 'audio_sha256'])
def test_recording_or_audio_anchor_closes_component_without_franchise_expansion(field):
    source = [{'id': 'a', 'artist': 'Artist A', 'audio_sha256': 'hash', 'group': 'franchise'},
              {'id': 'b', 'artist': 'Artist A', 'group': 'franchise'},
              {'id': 'c', 'artist': 'Artist C', 'group': 'franchise'},
              {'id': 'd', 'group': 'franchise'}]
    target = [{'id': 'other', field: source[0][field]}]
    mask, audit = exclusion(source, target)
    assert mask.tolist() == [False, False, True, True]
    assert audit['additional_excluded_ids'] == ['b']


def test_title_only_collision_excludes_row_without_expanding_artist_component():
    source = [{'id': 'a', 'artist': 'Artist A', 'title': 'Sóng'},
              {'id': 'b', 'artist': 'Artist A', 'title': 'Different'},
              {'id': 'c', 'artist': 'Artist C', 'title': 'Other'}]
    mask, audit = exclusion(source, [{'id': 'heldout', 'title': 'Song'}])
    assert mask.tolist() == [False, True, True]
    assert audit['anchored_components'] == []
    assert audit['additional_excluded_ids'] == []


def test_missing_identities_do_not_connect_unknown_rows_or_empty_corpora():
    source = [{'id': 'a'}, {'id': 'b', 'artist': '', 'audio_sha256': None}]
    mask, audit = exclusion(source, [{'id': 'heldout', 'artist': None}])
    assert mask.tolist() == [True, True]
    assert audit['missing_artist_rows'] == {'source': 2, 'target': 1}
    assert exclusion(source, [])[0].tolist() == [True, True]
    assert exclusion([], [])[0].shape == (0,)


def test_artist_alias_credit_and_audio_chain_are_transitive():
    source = [{'id': 'a', 'artist': 'Smith, Jane+Artist B'},
              {'id': 'b', 'artist': 'Artist B', 'audio_sha256': 'same'},
              {'id': 'c', 'audio_sha256': 'same'},
              {'id': 'd', 'artist': 'Another'}]
    assert exclusion(source, [{'id': 'heldout', 'artist': 'Jane Smith'}])[0].tolist() == [False, False, False, True]
