"""Pool admission accounts for all encoded bytes and still verifies content."""
from dataclasses import replace
import hashlib

import pytest

from yt_playlist.core import recommendation_plan as plans, recommendation_pools as pools


def prepared(container=list):
    return pools.prepare('pool', 'policy', None, 'a' * 64, 'radio',
        container([pools.Candidate(7, 1, 12, 'house'), pools.UnresolvedCandidate('absent|artist', -0.0, 12, 'house')]))


def encoded_rows():
    return [
        ('["recommendation-pool-1","pool","policy",null,"' + 'a' * 64 + '","radio",2]').encode(),
        b'[["recording",7],0,1.0,12,"house"]',
        b'[["unresolved","absent|artist"],1,0.0,12,"house"]',
    ]


def test_plan_budget_counts_pool_header_and_counts_shared_content_once(monkeypatch):
    pool = prepared()
    plan = plans.Plan('generation', 'policy', None, 0, 10,
        (plans.Slot('radio', 'first', 'membership', pool),
         plans.Slot('radio', 'second', 'membership', pool)))
    size = sum(map(len, encoded_rows()))
    monkeypatch.setattr(plans, 'MAX_PLAN_BYTES', size)
    assert plans.validate(plan)
    monkeypatch.setattr(plans, 'MAX_PLAN_BYTES', size - 1)
    with pytest.raises(ValueError, match='memory'):
        plans.validate(plan)


@pytest.mark.parametrize('container', [list, tuple, iter], ids=['list', 'tuple', 'iterator'])
def test_pool_budget_still_rejects_the_first_excess_byte(monkeypatch, container):
    size = sum(map(len, encoded_rows()[1:]))
    monkeypatch.setattr(pools, 'MAX_POOL_BYTES', size)
    pool = prepared(container)
    assert pool.candidates[-1].score == 0.0
    assert pool.content_digest == hashlib.sha256(b'\n'.join(encoded_rows()) + b'\n').hexdigest()
    assert pools.validate(pool) == sum(map(len, encoded_rows()))
    monkeypatch.setattr(pools, 'MAX_POOL_BYTES', size - 1)
    with pytest.raises(ValueError, match='membership budget'):
        prepared(container)


@pytest.mark.parametrize('limit', ['bytes', 'candidates'])
def test_unknown_length_input_stops_at_its_first_excess_row(monkeypatch, limit):
    first, second, unread = (pools.Candidate(identity) for identity in (1, 2, 3))
    candidates = iter((first, second, unread))
    if limit == 'bytes':
        monkeypatch.setattr(pools, 'MAX_POOL_BYTES', len(b'[["recording",1],0,null,null,null]'))
    else:
        monkeypatch.setattr(pools, 'MAX_CANDIDATES', 1)
    with pytest.raises(ValueError, match='membership budget'):
        pools.prepare('pool', 'policy', None, 'a' * 64, 'card', candidates)
    assert next(candidates) is unread


def test_plan_rejects_changed_candidates_with_a_copied_digest():
    pool = prepared()
    changed = replace(pool, candidates=(replace(pool.candidates[0], score=.5), *pool.candidates[1:]))
    plan = plans.Plan('generation', 'policy', None, 0, 10,
        (plans.Slot('radio', 'first', 'membership', changed),))
    with pytest.raises(ValueError, match='content identity changed'):
        plans.validate(plan)
