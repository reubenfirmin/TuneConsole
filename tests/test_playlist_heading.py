"""Generated heading metadata must not reinterpret ordinary or custom names."""
import pytest

from yt_playlist.web.playlist_heading import generated_heading


@pytest.mark.parametrize('title,name,kind,date,label,version', [
    ('Radio Session · 2026-10-21', 'Radio Session', 'Radio Session',
     '2026-10-21', 'October 21, 2026', None),
    ('Radio Session · 2026-10-26 #3', 'Radio Session', 'Radio Session',
     '2026-10-26', 'October 26, 2026', '3'),
    ('More in your wheelhouse - October 20 2026 #1', 'More in your wheelhouse',
     'More in your wheelhouse', '2026-10-20', 'October 20, 2026', '1'),
    ('Metal - Fresh songs - October 26 2026', 'Metal', 'Fresh songs',
     '2026-10-26', 'October 26, 2026', None),
    ('Post-rock - Comfort listening - February 29 2024 #12', 'Post-rock',
     'Comfort listening', '2024-02-29', 'February 29, 2024', '12'),
    ('Rock & "more" - From your catalog - October 27 2026 #2', 'Rock & "more"',
     'From your catalog', '2026-10-27', 'October 27, 2026', '2'),
])
def test_generated_heading_separates_name_type_date_and_version(title, name, kind, date, label, version):
    assert generated_heading(title, is_generated=True) == {
        'name': name, 'kind': kind, 'date': date, 'date_label': label, 'version': version}


@pytest.mark.parametrize('title', [
    'Metal - Fresh songs - February 30 2026 #1',
    'Metal - Fresh songs - February 29 2025 #1',
    'Metal - Something custom - October 26 2026 #1',
    'My own renamed mix',
    '',
])
def test_invalid_or_custom_generated_titles_remain_verbatim(title):
    assert generated_heading(title, is_generated=True) is None


@pytest.mark.parametrize('title', [
    'Metal - Fresh songs - October 26 2026 #1', 'Radio Session · 2026-10-21'])
def test_an_ordinary_playlist_is_not_interpreted_as_generated(title):
    assert generated_heading(title, is_generated=False) is None
