"""Connected reading paths, responsive spreads and the shared sidebar footer."""
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from yt_playlist.rec import home_prepared, into_recently


def _art(index):
    """Local cover fixtures keep the visual check independent of image providers."""
    colors = ['#445d67', '#9c7b55', '#775a79', '#426b63', '#8c5d55', '#596089']
    color = colors[index % len(colors)]
    return 'data:image/svg+xml,' + quote(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 450">
      <defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="{color}"/>
      <stop offset="1" stop-color="#172126"/></linearGradient></defs>
      <path fill="url(#g)" d="M0 0h600v450H0z"/><circle cx="{190 + index * 20}" cy="240" r="155"
      fill="none" stroke="#e2d6b2" stroke-opacity=".6" stroke-width="1"/>
      <path d="M0 360Q230 50 600 290M0 400Q330 140 600 340" fill="none" stroke="#dad1b9" stroke-opacity=".3"/>
      </svg>''')


@pytest.mark.parametrize('width', [1440, 1000, 390])
def test_story_collection_has_clear_reading_links_and_room_to_scroll(live_app, store, page, width):
    names = ['Minimal', 'Robert Hood', 'Daniel Bell', 'Dub techno', 'Basic Channel', 'Floating Points',
             'Shoegaze', 'Slowdive', 'My Bloody Valentine']
    extracts = [
        'Minimal techno is a form of dance music built around a restrained palette of sounds. Small '
        'changes in rhythm, timbre, and space become the focus, with repetition making each shift audible. '
        'Its stripped arrangements leave room for the listener to hear the movement inside the loop.',
        'Robert Hood is an American electronic musician and producer from Detroit. His work helped shape '
        'minimal techno, using precise rhythms and carefully chosen sounds to give spare arrangements '
        'their momentum. He was a founding member of the collective Underground Resistance.',
        'Daniel Bell is an American electronic musician known for a spare, rhythmic approach to techno. '
        'Working as DBX, he made tightly focused tracks in which a few sounds shift against a persistent groove.',
        'Dub techno combines the repetition and structure of techno with the production techniques of dub. '
        'Chords dissolve into echoes, while delay and reverberation become instruments in their own right.',
        'Basic Channel was a Berlin production partnership whose records helped define dub techno. '
        'Their music places repeating chords inside shifting layers of delay, hiss, and reverberation.',
        'Sam Shepherd, known professionally as Floating Points, is a British electronic musician and composer. '
        'His recordings move between intricate dance music, jazz, and expansive orchestral arrangements.',
        'Shoegaze is a subgenre of alternative and indie rock characterized by its ethereal mixture of '
        'obscured vocals, guitar distortion and effects, feedback, and overwhelming volume. The name '
        'came from the performers’ still, introspective stage presence and their attention to effects pedals.',
        'Slowdive are an English band formed in Reading in 1989. Their recordings combine softly voiced '
        'melodies with layers of guitar effects, creating an expansive and atmospheric sound.',
        'My Bloody Valentine are a band formed in Dublin in 1983. Their music combines heavily processed '
        'guitars, obscured vocals, and unusual production techniques, giving familiar pop forms a blurred surface.',
    ]
    cards = []
    for i, name in enumerate(names):
        genre = i in (0, 3, 6)
        kind = 'genre' if genre else 'artist'
        anchor = [0, 0, 0, 3, 3, 5, 6, 6, 6][i]
        anchor_kind = 'genre' if anchor in (0, 3, 6) else 'artist'
        cards.append({'kind': kind, 'subject': f'{kind}:{name.lower()}',
            'group': f'{anchor_kind}:{names[anchor].lower()}',
            'display': name, 'extract': extracts[i],
            'thumbnail': None if i in (0, 3) else _art(i),
            'lead_allowed': not genre or i == 6,
            'url': f'https://en.wikipedia.org/wiki/{name.replace(" ", "_")}',
            'color': 'var(--genre-techno)' if genre else 'var(--accent-2)',
            'seed': name, 'genre': name.lower() if genre else None, 'depth': 2,
            'songs': [{'title': title, 'artist': name if not genre else 'Artist & Guest',
                       'video_id': f'story-{i}-{j}', 'thumbnail': _art(i + j) if j != 2 else None}
                      for j, title in enumerate(['A Slow Unfolding', 'Patterns in the Light',
                                                  'The Very Long Name of a Song in Three Parts'])]})
    store.rec_proposals.put_proposals(into_recently.SURFACE,
        {'schema': into_recently.SCHEMA, 'built_at': 1, 'cards': cards, 'pending': False}, 1)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/playlists')
    page.evaluate("""async () => {
        const main = document.querySelector('main');
        main.classList.add('page-home');
        await htmx.ajax('GET', '/home/into-recently', {target: main, swap: 'innerHTML'});
    }""")
    expect(page.locator('.into-card')).to_have_count(9)
    reading_links = page.get_by_role('link', name='Read more about', exact=False)
    expect(reading_links).to_have_count(9)
    expect(reading_links.first).to_have_text('Read more')
    expect(page.locator('.into-feature')).to_have_count(4)
    expect(page.locator('.into-spread')).to_have_count(4)
    expect(page.locator('.into-linked')).to_have_count(3)
    minimal = page.locator('.into-spread[data-thread="artist:robert hood"]')
    expect(minimal.locator('h3')).to_have_text(['Robert Hood', 'Minimal', 'Daniel Bell'])
    dub = page.locator('.into-spread[data-thread="artist:basic channel"]')
    expect(dub.locator('h3')).to_have_text(['Basic Channel', 'Dub techno'])
    expect(page.locator('.into-feature-spread h3')).to_have_text('Floating Points')
    expect(page.locator('.into-feature[data-kind="genre"] h3')).to_have_text('Shoegaze')
    expect(page.locator('.into-feature[data-kind="genre"] .into-art img')).to_have_count(1)
    expect(page.locator('.into-text-only')).to_have_count(2)
    expect(page.locator('.into-heading')).to_have_count(0)
    expect(page.locator('.into-song')).to_have_count(27)
    for card in page.locator('.into-card').all():
        expect(card.locator('.into-song-play')).to_have_count(3)
        assert card.locator('.into-extract').evaluate('el => el.scrollHeight <= el.clientHeight + 1')
    first_play = page.get_by_role('link', name='Play A Slow Unfolding by Robert Hood on YouTube Music')
    first_play.focus()
    expect(first_play.locator('svg')).to_have_css('opacity', '1')
    expect(first_play).to_have_css('outline-width', '2px')
    page.evaluate('window.tcPlay = url => { window.storyPlayback = url; }')
    first_play.click()
    assert page.evaluate('window.storyPlayback') == 'https://music.youtube.com/watch?v=story-1-0'
    assert page.locator('.into-card').last.bounding_box()['y'] > 1000
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    for song in page.locator('.into-song').all():
        box = song.bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= width
    if width == 390:
        for card in page.locator('.into-card').all():
            boxes = [song.bounding_box() for song in card.locator('.into-song').all()]
            assert boxes[0]['y'] < boxes[1]['y'] < boxes[2]['y']
    privacy = page.get_by_role('link', name='Privacy', exact=True)
    expect(privacy).to_have_count(1)
    expect(privacy).to_have_attribute('href', '/privacy')
    expect(privacy).to_be_visible()
    if width >= 1040:
        assert privacy.bounding_box()['y'] > 900
    page.mouse.move(0, 0)
    page.evaluate('document.activeElement.blur()')
    expect(first_play.locator('svg')).to_have_css('opacity', '0')
    page.locator('.into-grid img').evaluate_all("imgs => imgs.forEach(img => img.loading = 'eager')")
    page.wait_for_function("[...document.querySelectorAll('.into-grid img')].every(img => img.complete)")
    page.screenshot(path=f'/tmp/tuneconsole-stories-{width}.png', full_page=True, animations='disabled')
    page.screenshot(path=f'/tmp/tuneconsole-stories-{width}-top.png', animations='disabled')


def test_story_cooldown_only_counts_visible_headings_and_ignores_fragment_polls(live_app, store, page):
    cards = [{'kind': 'artist', 'subject': f'artist:Band {i}', 'display': f'Band {i}',
              'extract': 'A full paragraph about the band. ' * 30, 'thumbnail': _art(i),
              'url': f'http://wiki/Band_{i}', 'color': 'var(--accent)', 'seed': f'Band {i}',
              'genre': None, 'depth': 2, 'songs': []} for i in range(3)]
    store.rec_proposals.put_proposals(into_recently.SURFACE,
        {'schema': into_recently.SCHEMA, 'built_at': 1, 'cards': cards, 'pending': False}, 1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    assert home_prepared.prepare(store, 1) is not None
    page.set_viewport_size({'width': 1000, 'height': 650})
    page.goto(live_app + '/')
    headings = page.locator('[data-story-subject]')
    expect(headings).to_have_count(3)
    expect(headings.first).to_be_hidden()
    page.wait_for_timeout(1100)
    assert store.rec_ledger.story_views() == {}
    page.get_by_role('tab', name='Into recently', exact=True).click()
    headings.first.scroll_into_view_if_needed()
    page.wait_for_timeout(1100)
    views = store.rec_ledger.story_views()
    assert views['artist:Band 0']['views'] == 1
    assert 'artist:Band 2' not in views
    page.evaluate("""async () => htmx.ajax('GET', '/home/into-recently', {
        target: '[data-dash="recent"] > div', swap: 'innerHTML'})""")
    headings.first.scroll_into_view_if_needed()
    page.wait_for_timeout(1100)
    assert store.rec_ledger.story_views()['artist:Band 0']['views'] == 1
    headings.last.scroll_into_view_if_needed()
    page.wait_for_timeout(1100)
    assert store.rec_ledger.story_views()['artist:Band 2']['views'] == 1
