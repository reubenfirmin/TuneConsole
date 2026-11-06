"""Only the serial unlock frontier gets a map byline."""
import pytest

from yt_playlist.rec import account_growth


@pytest.mark.parametrize('blocked', range(len(account_growth.MAIN_PATH) + 1))
def test_only_next_locked_tent_is_upcoming_even_when_downstream_output_is_ready(blocked):
    nodes = [account_growth._node(identity, identity, ready=index != blocked, used=index < blocked)
             for index, identity in enumerate(account_growth.MAIN_PATH)]
    nodes += [account_growth._node(identity, identity, ready=True, used=True, bonus=True)
              for identity in ('lastfm', 'takeout', 'deepseek', 'milestones')]
    account_growth._connect_paths(nodes)
    upcoming = [node['id'] for node in nodes if node['upcoming']]
    assert upcoming == ([account_growth.MAIN_PATH[blocked]] if blocked < len(account_growth.MAIN_PATH) else [])
    assert all(not node['upcoming'] for node in nodes if node['used'] or node['optional'])


def test_earned_access_during_refresh_does_not_add_bylines_to_visited_tents():
    nodes = [account_growth._node(identity, identity, ready=False, used=index <= 7)
             for index, identity in enumerate(account_growth.MAIN_PATH)]
    account_growth._connect_paths(nodes, resume=('vibes',))
    assert [node['id'] for node in nodes if node['upcoming']] == ['recent']
