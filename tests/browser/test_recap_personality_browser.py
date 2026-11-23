"""The recap's energy wording reflects the evidence that produced its score."""
import pytest
from playwright.sync_api import expect

from yt_playlist.rec import recap_story


@pytest.mark.parametrize(('measured', 'expected'), [
    (243, 'The tracks you played leaned high-energy, based on audio measurements.'),
    (0, 'Your genres skew high-energy.'),
    (91, 'Your listening leaned high-energy, based on genre estimates and audio measurements for 91 of 243 plays.'),
], ids=['measured', 'genre', 'mixed'])
def test_personality_describes_energy_evidence(live_app, store, page, measured, expected):
    month = '2026-10'
    beat = recap_story.personality({'plays': 243},
        {'measured': ('Artist', 'techno'), 'unmeasured': ('Artist', 'techno')},
        track_plays={'measured': measured, 'unmeasured': 243 - measured},
        audio={'measured': {'energy': .8}}, hours=[0] * 24,
        genre_axes={'techno': ('techno', None)})
    assert beat['axes']['energy'] == .8
    store.rec_proposals.put_proposals('trend_rollups', {'story': {
        'month': month, 'month_name': 'October', 'beats': [beat],
    }}, now=1794096000.0)

    page.set_viewport_size({'width': 1440, 'height': 1000})
    assert page.goto(f'{live_app}/trends/story/{month}').status == 200
    slide = page.get_by_role('group', name='personality', exact=True)
    expect(slide).to_be_visible()
    expect(slide.locator('.slide-sub')).to_contain_text(expected)
