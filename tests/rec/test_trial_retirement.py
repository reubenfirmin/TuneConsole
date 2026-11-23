import pytest

from yt_playlist.rec import choice_evidence


@pytest.mark.parametrize('format', ['albums', 'mixes'])
def test_interleaved_views_only_close_the_requesting_offer(store, format):
    offers = [choice_evidence.present(store, format, [{'id': 'option'}], 100 + index,
              context={}, window=index, episode_id=f'view-{index}') for index in range(2)]
    for identity in offers:
        choice_evidence.respond(store, identity, 'visible', 102, visible=['option'])
    assert store.rec_proposals.get_proposals('choice_window:' + format)['opportunity_id'] == offers[1]
    choice_evidence.close_window(store, format, 103, opportunity_id=offers[0])
    choice_evidence.close_window(store, format, 104, opportunity_id=offers[0])
    closes = [row for row in store.rec_ledger.evidence().records('response') if row['kind'] == 'close']
    assert [row['opportunity_id'] for row in closes] == [offers[0]]
