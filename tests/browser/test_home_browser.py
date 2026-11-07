import pytest
from tests.test_mode_cards_route import prepare_home as prepare_current_home
from tests.home_fixture_support import browser_store as store, returning_listener
from tests.prepared_fixture_support import prepared_template, roots
from tests.visual_support import capture


def prepare_home(store, now=1):
    # These are post-unlock consumers, including recovery with fewer than four
    # current offers. Prior use keeps access; current output still drives slots.
    returning_listener(store, 'mixes', now=now)
    prepare_current_home(store, now)


@pytest.fixture(scope='module')
def genre_home_template(_pristine_current_schema):
    from tests.test_home_other_genres import seed_genres

    def seed(source):
        seed_genres(source)
        prepare_home(source, 1)
        return source.rec_proposals.get_proposals('home_prepared')

    with prepared_template(_pristine_current_schema, seed) as template:
        yield template


@pytest.fixture
def prepared_genre_app(request, store, genre_home_template):
    # Restore before the live app creates its own account, playlist and jobs.
    genre_home_template.restore(store)
    return request.getfixturevalue('live_app')


@pytest.fixture(scope='module')
def mixtape_home_template(_pristine_current_schema):
    from tests.home_prepared_support import seed

    def build(source):
        # Preserve this pair's full eight-track input; the genre consumers and
        # the smaller session Home template exercise different baselines.
        seed(source)
        prepare_home(source, 1)
        payload = source.rec_proposals.get_proposals('home_prepared')
        assert payload and len(payload['menus'][0]['protos']) == 4
        return payload

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def prepared_mixtape_app(request, store, mixtape_home_template):
    mixtape_home_template.restore(store)
    assert not store.accounts.get_accounts()
    # The live app owns its account, playlist and jobs after the private restore.
    return request.getfixturevalue('live_app')


@pytest.fixture
def home_sibling_store(tmp_path, _pristine_current_schema):
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core.store import Store

    # File owners use the database directory, so siblings need separate parents.
    directory = tmp_path / 'sibling'
    directory.mkdir()
    sibling = Store(directory / 'home.db')
    try:
        initialize_consumer(sibling, _pristine_current_schema)
        yield sibling
    finally:
        sibling.conn.close()




@pytest.fixture
def expiring_pool_jobs(monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.jobs import SyncJobs

    jobs = SyncJobs(completed_limit=0)
    monkeypatch.setattr(webapp, "SyncJobs", lambda: jobs)
    return jobs


@pytest.mark.parametrize("kind,lane", [("fresh_pool", "fresh")])
def test_expired_pool_stops_polling_and_refreshes_mixes(
        expiring_pool_jobs, prepared_genre_app, store, page, kind, lane,
        genre_home_template, home_sibling_store):
    import sqlite3
    from playwright.sync_api import expect

    live_app = prepared_genre_app
    if kind == 'fresh_pool':
        template = genre_home_template
        sibling = template.restore(home_sibling_store)
        assert sibling == template.result == store.rec_proposals.get_proposals('home_prepared')
        assert template.source.accounts.get_accounts() == []
        assert len(store.accounts.get_accounts()) == 1
        assert len({template.source._evidence_key, store._evidence_key, home_sibling_store._evidence_key}) == 3
        for current in (template.source, store, home_sibling_store):
            assert current.recommendations.heads() == template.source.recommendations.heads()
            assert list(map(tuple, current.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id'))) == list(map(
                tuple, template.source.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id')))
            assert len(current.modes.list_modes(active_only=True)) == 6
            assert current.conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert set(template.bundles) == {'all', '_meta', '0', '1', '2', '3', '4', '5'}
        for mode in map(str, range(6)):
            assert all(len(items) == 20 for items in template.bundles[mode].values())
        destinations = [roots(target.conn) for target in (store, home_sibling_store)]
        copied = 0
        for name, directory in template.files.items():
            assert len({directory, *(owned[name] for owned in destinations)}) == 3
            for source in directory.rglob('*'):
                if source.is_file():
                    paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
                    assert all(path.read_bytes() == source.read_bytes() for path in paths)
                    assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
                    copied += 1
        assert copied
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            with template.source.conn:
                template.source.conn.execute("INSERT INTO settings VALUES ('private','forbidden')")
        sibling['menus'].clear()
        home_sibling_store._fixture_bundle_payload['all']['wheelhouse'].clear()
        home_sibling_store.settings.set_setting('private_home', 'sibling')
        assert template.result['menus'] and store.rec_proposals.get_proposals('home_prepared')['menus']
        assert len(store._fixture_bundle_payload['all']['wheelhouse']) == 120
        assert len(template.bundles['all']['wheelhouse']) == 120
        assert template.source.settings.get_setting('private_home') is None
        assert store.settings.get_setting('private_home') is None

    job = expiring_pool_jobs.create(kind)
    job.parameters = {"include_genres": [], "avoid_genres": [], "lane": lane, "epoch": 0}
    path = f"/home/{kind.replace('_', '-')}/{job.id}"
    page.goto(f"{live_app}/playlists")
    page.evaluate("""async path => {
        const board = document.createElement('div');
        board.id = 'home-mode-cards';
        board.setAttribute('x-data', '{focus: null}');
        document.body.append(board);
        await htmx.ajax('GET', path, {target: '#home-mode-cards', swap: 'innerHTML'});
    }""", path)
    card = page.locator("#home-mode-cards .fresh-pool")
    expect(card).to_have_attribute("aria-busy", "true")
    job.done = True
    assert expiring_pool_jobs.get(job.id) is None
    with page.expect_response(lambda response: response.url.endswith(path)) as poll:
        pass
    assert poll.value.status == 200
    expect(card).to_have_attribute("aria-busy", "false")
    expect(card.get_by_role("status")).to_contain_text("no longer available")
    assert card.get_attribute("hx-get") is None
    polls = []
    page.on("request", lambda request: polls.append(request.url) if request.url.endswith(path) else None)
    with page.expect_response("**/home/refresh-cards") as refreshed:
        card.get_by_role("button", name="Refresh mixes", exact=True).click()
    assert refreshed.value.status == 200
    expect(page.locator("#home-mode-cards > .gen-card")).to_have_count(4)
    # Wait longer than the old polling interval to check HTMX removed its timer.
    page.wait_for_timeout(2200)
    assert polls == []














def test_home_is_landing_with_status_and_sections(live_app, page):
    from playwright.sync_api import expect

    page.goto(f"{live_app}/")
    # Home is the default tab. Syncing is automatic in the background now, so there is no manual
    # sync button. On this live_app (no extension, never synced) the landing is the
    # welcome; the feed and the freshness line wait for a first sync.
    assert page.get_by_role("button", name="Full sync").count() == 0
    assert page.get_by_role("button", name="Sync plays").count() == 0
    expect(page.get_by_role("heading", name="Welcome to TuneConsole")).to_be_visible()
    expect(page.get_by_role("link", name="Get the extension")).to_be_visible()
    expect(page.get_by_role('heading', name='How do you like to listen?')).to_have_count(0)
    assert page.get_by_text("Library synced").count() == 0     # no awkward "not yet" line anymore






def test_genre_controls_make_mixes_in_the_tabbed_configurator(prepared_genre_app, store, page, tmp_path):
    from playwright.sync_api import expect
    live_app = prepared_genre_app
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    # Keep the browser check local: album art is incidental to this control.
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    panel = page.locator("#fingerprint")
    expect(panel).to_have_class("fingerprint card is-collapsed")
    button = panel.locator("#home-make-mixes")
    expect(button).to_be_hidden()
    panel.locator(".fp-collapse").click()
    expect(page.get_by_role("tab", name="Vibe", exact=True)).to_be_hidden()
    expect(page.get_by_role("tab", name="Genre focus", exact=True)).to_have_attribute("aria-selected", "true")
    expect(button).to_be_enabled()
    choices = panel.get_by_role("checkbox")
    expect(choices).to_have_count(6)
    expect(button).to_have_accessible_name("Shuffle genres")
    expect(panel.locator("#fp-mixes-panel button")).to_have_count(1)
    before = set(page.evaluate("homeOfferedGenres()"))
    assert len(before) == 4
    search = panel.get_by_role("searchbox", name="Find a genre for these mixes")
    launcher = page.get_by_role("button", name="Tune these mixes", exact=True)
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    expect(dialog).to_be_visible()
    assert dialog.evaluate("el => el.matches(':modal')")
    for _ in range(24):
        page.keyboard.press("Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    for _ in range(24):
        page.keyboard.press("Shift+Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.locator("header nav").get_by_role("link", name="Playlists", exact=True).evaluate("el => el.focus()")
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.set_viewport_size({"width": 1280, "height": 850})
    search.fill("jazz")
    search.focus()
    expect(search).to_have_value("jazz")
    assert search.evaluate("""el => {
        const css = getComputedStyle(el);
        return css.color === getComputedStyle(el.closest('dialog')).color
            && css.outlineStyle === 'none' && css.boxShadow === 'none';
    }""")
    expect(button).to_be_in_viewport()
    capture(page, path=tmp_path / "mix-modal-1280.png")
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(launcher).to_be_focused()
    launcher.click()
    expect(dialog).to_be_visible()
    # The real backdrop must not activate an underlying card or navigation.
    page.mouse.click(2, 2)
    expect(dialog).not_to_be_visible()
    expect(launcher).to_be_focused()
    assert page.url == f"{live_app}/"
    expect(page.locator(".gen-card.is-focused")).to_have_count(0)
    launcher.click()
    search.fill("nothing-matches")
    expect(panel.get_by_text("No matching genres.", exact=True)).to_be_visible()
    search.fill("jazz")
    expect(panel.locator(".fp-genre-choice:visible")).to_have_count(1)
    jazz = panel.get_by_role("checkbox", name="jazz", exact=True)
    jazz.focus()
    jazz.press("Space")
    expect(jazz).to_be_checked()
    search.fill("")
    panel.get_by_role("checkbox", name="techno", exact=True).check()
    expect(button).to_have_accessible_name("Make mixes")
    assert set(page.evaluate("homeOfferedGenres()")) == before  # selections are a draft until applied
    jazz.focus()
    page.evaluate("document.dispatchEvent(new CustomEvent('htmx:afterSettle'))")
    expect(jazz).to_be_checked()
    expect(jazz).to_be_focused()
    expect(panel.get_by_role("checkbox", name="techno", exact=True)).to_be_checked()
    for width in (1280,):
        page.set_viewport_size({"width": width, "height": 850})
        panel.scroll_into_view_if_needed()
        expect(button).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        capture(page, path=tmp_path / f"mix-genres-{width}.png")
    page.route("**/home/mix-genres", lambda route: route.fulfill(status=500, body="Unavailable"))
    button.click()
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")
    expect(button).to_be_enabled()
    expect(jazz).to_be_enabled()
    expect(jazz).to_be_checked()
    expect(panel.get_by_role("checkbox", name="techno", exact=True)).to_be_checked()
    assert set(page.evaluate("homeOfferedGenres()")) == before
    page.evaluate("document.dispatchEvent(new CustomEvent('htmx:afterSettle'))")
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")
    page.unroute("**/home/mix-genres")
    button.click()
    expect(panel.locator("dialog")).not_to_be_visible()
    assert set(page.evaluate("homeOfferedGenres()")) == {"jazz", "techno"}
    panel.locator(".fp-collapse").click()
    expect(jazz).to_be_checked()
    expect(panel.get_by_role("checkbox", name="techno", exact=True)).to_be_checked()
    expect(button).to_be_enabled()
    expect(panel.locator(".fp-mix-fields")).to_be_enabled()
    # Toggling the selected chips off returns the same action to genre shuffle.
    jazz.uncheck()
    panel.get_by_role("checkbox", name="techno", exact=True).uncheck()
    expect(button).to_have_accessible_name("Shuffle genres")
    assert set(page.evaluate("homeOfferedGenres()")) == {"jazz", "techno"}
    before = set(page.evaluate("homeOfferedGenres()"))
    # HTMX restores a placeholder's old CSS classes until settling. Genre metadata
    # must already include the newly ready fourth card when the swap completes.
    page.evaluate("""() => {
        window.shuffledGenresBeforeSettle = null;
        document.addEventListener('htmx:afterSwap', event => {
            if (event.detail.xhr?.responseURL.endsWith('/home/other-genres')) {
                window.shuffledGenresBeforeSettle = homeOfferedGenres();
            }
        });
    }""")
    button.click()
    expect(panel.locator("dialog")).not_to_be_visible()
    during_swap = set(page.evaluate("window.shuffledGenresBeforeSettle"))
    assert len(during_swap) == 4 and not before & during_swap
    after = set(page.evaluate("homeOfferedGenres()"))
    assert after == during_swap
    panel.locator(".fp-collapse").click()
    expect(button).to_be_enabled()
    expect(panel.locator("#home-theme-summary")).to_be_empty()
    page.get_by_role("tab", name="Nerd out", exact=True).click()
    expect(button).to_be_hidden()
    expect(panel.locator(".fp-breadth-slider")).to_be_visible()
    preference_search = dialog.get_by_role("textbox", name="Find a genre to adjust")
    preference_search.fill("jazz")
    assert preference_search.evaluate("""el => {
        const css = getComputedStyle(el);
        return css.color === getComputedStyle(el.closest('dialog')).color
            && css.outlineStyle === 'none' && css.boxShadow === 'none'
            && css.borderTopWidth === '0px';
    }""")
    capture(page, path=tmp_path / "preferences-search.png")
    page.get_by_role("tab", name="Nerd out", exact=True).press("ArrowLeft")
    expect(page.get_by_role("tab", name="Genre focus", exact=True)).to_be_focused()
    for width in (1280,):
        page.set_viewport_size({"width": width, "height": 850})
        expect(button).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    panel.get_by_role("button", name="Close mix controls", exact=True).click()
    expect(button).to_be_hidden()

    panel.locator(".fp-collapse").click()
    page.route("**/home/other-genres", lambda route: route.fulfill(status=500, body="Unavailable"))
    button.click()
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")
    expect(button).to_be_enabled()
    expect(panel.locator("#fp-mixes-panel")).not_to_have_attribute("aria-busy", "true")
    assert set(page.evaluate("homeOfferedGenres()")) == after
