"""Browser coverage for the genre tag editor under Enrichment / Advanced.

The shared browser conftest seeds a playlist, not genres, and is off-limits, so
this module brings its own live_app fixture (the same ~12-line uvicorn boilerplate)
and relies on create_app seeding the built-in genre whitelist.
"""
import re
import json
from contextlib import contextmanager
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_app(store):
    # Background catalog preparation is disabled in these isolated app tests.
    store.genres.configure_catalog()
    iid = store.accounts.upsert_account("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def response_timings(page, tmp_path):
    timings = []

    def finished(request):
        if '/genres/' in request.url:
            timings.append({'url':request.url, 'method':request.method,
                            'timing':request.timing, 'sizes':request.sizes()})

    page.on('requestfinished', finished)
    yield
    page.remove_listener('requestfinished', finished)
    (tmp_path / 'genre-responses.json').write_text(json.dumps(timings, indent=2))


@contextmanager
def _completed_request(page, path):
    with page.expect_response(lambda response: response.url.split('?')[0].endswith(path)) as pending:
        yield
    response = pending.value
    assert response.status == 200
    assert response.finished() is None


def _count(page):
    """The whitelist size, read from the visible 'N genres' label."""
    text = page.get_by_text(re.compile(r"^\d+ genres$")).inner_text()
    return int(text.split()[0])


def _chip(page, name):
    """The chip whose label is exactly `name` (the inner text span)."""
    return page.get_by_text(name, exact=True)


def _open_editor(live_app, page):
    page.goto(f"{live_app}/enrich")
    expect(page.get_by_role("heading", name="Recognized genre tags")).to_be_hidden()
    page.get_by_text("Advanced", exact=True).click()
    expect(page.get_by_role("heading", name="Recognized genre tags")).to_be_visible()


@pytest.mark.parametrize("width", [1280, 390])
def test_legacy_link_opens_enrichment_editor(live_app, page, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_app}/genres")
    expect(page).to_have_url(f"{live_app}/enrich?advanced=genres#recognized-genre-tags")
    assert page.get_by_role("heading", name="Enrichment", exact=True).is_visible()
    assert page.get_by_role("heading", name="Recognized genre tags").is_visible()
    expect(page.locator('.tools-pop a[href="/genres"]')).to_have_count(0)
    house = page.get_by_role("region", name="House", exact=True)
    house.locator("summary").first.click()
    expect(house.get_by_text("Deep House", exact=True)).to_be_visible()
    expect(house.get_by_text("Jazz", exact=True)).to_have_count(0)
    unassigned = page.get_by_role("region", name="No family assigned")
    expect(unassigned.get_by_text("Christian", exact=True)).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=tmp_path / f"genre-hierarchy-{width}.png", full_page=True)


def test_add_via_button_adds_chip_and_bumps_count(live_app, page):
    _open_editor(live_app, page)
    before = _count(page)
    page.get_by_role("textbox", name="Genre tag", exact=True).fill("Whack-a-doodle")
    with _completed_request(page, '/genres/add'):
        page.get_by_role("button", name="Add", exact=True).click()
    # The POST replaces the list, including its disclosure elements. Expand
    # the new list only once its updated count proves the swap completed.
    expect(page.get_by_text(re.compile(r"^\d+ genres$"))).to_have_text(f"{before + 1} genres")
    page.get_by_role("region", name="No family assigned").locator("summary").first.click()
    _chip(page, "Whack-a-doodle").wait_for(state="visible", timeout=3000)
    expect(page.get_by_role("region", name="No family assigned").get_by_text(
        "Whack-a-doodle", exact=True)).to_be_visible()
    assert _count(page) == before + 1


def test_add_via_enter_adds_chip(live_app, page, store):
    _open_editor(live_app, page)
    inp = page.get_by_role("textbox", name="Genre tag", exact=True)
    inp.fill("Synthwave")
    with _completed_request(page, '/genres/list'):
        with _completed_request(page, '/genres/add'):
            inp.press("Enter")
            page.get_by_role("searchbox", name="Find a genre, alias or family").fill("Synthwave")
    _chip(page, "Synthwave").wait_for(state="visible", timeout=3000)
    assert 'Synthwave' in store.genres.get_genre_whitelist()
    expect(_chip(page, 'Deep House')).to_have_count(0)


def test_remove_chip_drops_it_and_lowers_count(live_app, page, store):
    _open_editor(live_app, page)
    before = _count(page)
    page.get_by_role("region", name="Classic Rock", exact=True).locator("summary").first.click()
    # the chip's × button is the sibling of its label span (markup shared across the refactor)
    with _completed_request(page, '/genres/remove'):
        page.get_by_role('button', name='Remove Rock', exact=True).click()
    _chip(page, "Rock").wait_for(state="hidden", timeout=3000)
    assert _count(page) == before - 1
    assert store.conn.execute("SELECT enabled FROM genre_preferences WHERE genre_key='rock'").fetchone()[0] == 0


def test_reset_restores_defaults(live_app, page, store):
    _open_editor(live_app, page)
    inp = page.get_by_role("textbox", name="Genre tag", exact=True)
    inp.fill("Zzztestgenre")
    defaults = store.genres.get_genre_whitelist()
    with _completed_request(page, '/genres/list'):
        with _completed_request(page, '/genres/add'):
            inp.press("Enter")
            page.get_by_role("searchbox", name="Find a genre, alias or family").fill("Zzztestgenre")
    _chip(page, "Zzztestgenre").wait_for(state="visible", timeout=3000)
    with _completed_request(page, '/genres/reset'):
        page.get_by_role("button", name="Reset to defaults").click()
    _chip(page, "Zzztestgenre").wait_for(state="hidden", timeout=3000)   # custom genre gone
    with _completed_request(page, '/genres/list'):
        page.get_by_role("searchbox", name="Find a genre, alias or family").fill("Rock")
    _chip(page, "Rock").wait_for(state="visible", timeout=3000)          # built-ins restored
    assert store.genres.get_genre_whitelist() == defaults


def test_alias_search_and_removal_keep_the_filter(live_app, page):
    _open_editor(live_app, page)
    with _completed_request(page, '/genres/list'):
        page.get_by_role("searchbox", name="Find a genre, alias or family").fill("rap")
    _chip(page, "Hip Hop").wait_for(state="visible")
    expect(_chip(page, "Deep House")).to_have_count(0)
    hiphop = _chip(page, "Hip Hop").locator("..")
    hiphop.locator(".genre-aliases summary").click()
    expect(hiphop.locator(".genre-aliases p")).to_contain_text("rap")
    with _completed_request(page, '/genres/remove'):
        hiphop.get_by_role("button", name="Remove Hip Hop").click()
    expect(_chip(page, "Hip Hop")).to_have_count(0)
    expect(page.get_by_role("searchbox", name="Find a genre, alias or family")).to_have_value("rap")


def test_rapid_adds_and_filter_keep_both_preferences(live_app, page, store, tmp_path):
    _open_editor(live_app, page)
    pending = []

    def hold_first(route):
        if not pending:
            pending.append(route)
        else:
            route.continue_()

    page.route('**/genres/add', hold_first)
    inp = page.get_by_role('textbox', name='Genre tag', exact=True)
    inp.fill('Rapid first')
    with page.expect_request('**/genres/add'):
        inp.press('Enter')
    # Keep the first mutation in flight while the user edits the stable input.
    inp.fill('Rapid second')
    inp.press('Enter')
    with _completed_request(page, '/genres/list'):
        page.get_by_role('searchbox', name='Find a genre, alias or family').fill('Rapid')
        pending[0].continue_()
    expect(_chip(page, 'Rapid first')).to_be_visible()
    expect(_chip(page, 'Rapid second')).to_be_visible()
    assert {'Rapid first','Rapid second'} <= set(store.genres.get_genre_whitelist())
    with _completed_request(page, '/genres/remove'):
        page.get_by_role('button', name='Remove Rapid first', exact=True).click()
    expect(_chip(page, 'Rapid first')).to_have_count(0)
    expect(_chip(page, 'Rapid second')).to_be_visible()
    assert 'Rapid first' not in store.genres.get_genre_whitelist()
    page.screenshot(path=tmp_path / 'rapid-filtered-edit.png', full_page=True)


def test_three_submissions_preserve_each_name_while_first_is_pending(live_app, page, store):
    _open_editor(live_app, page)
    held = []

    def hold_first(route):
        if not held:
            held.append(route)
        else:
            route.continue_()

    page.route('**/genres/add', hold_first)
    field = page.get_by_role('textbox', name='Genre tag', exact=True)
    names = ['Queued first', 'Queued second', 'Queued third']
    field.fill(names[0])
    with page.expect_request('**/genres/add'):
        field.press('Enter')
    for name in names[1:]:
        field.fill(name)
        field.press('Enter')
    with _completed_request(page, '/genres/list'):
        page.get_by_role('searchbox', name='Find a genre, alias or family').fill('Queued')
        held[0].continue_()
    assert set(names) <= set(store.genres.get_genre_whitelist())
    for name in names:
        expect(_chip(page, name)).to_be_visible()


def test_two_removals_survive_the_first_list_replacement(live_app, page, store):
    names = ['Queued removal first', 'Queued removal second']
    for name in names:
        store.genres.add_genre(name)
    _open_editor(live_app, page)
    search = page.get_by_role('searchbox', name='Find a genre, alias or family')
    with _completed_request(page, '/genres/list'):
        search.fill('Queued removal')
    held = []

    def hold_first(route):
        if not held:
            held.append(route)
        else:
            route.continue_()

    page.route('**/genres/remove', hold_first)
    with page.expect_request('**/genres/remove'):
        page.get_by_role('button', name=f'Remove {names[0]}', exact=True).click()
    page.get_by_role('button', name=f'Remove {names[1]}', exact=True).click()
    with _completed_request(page, '/genres/list'):
        search.fill('Queued')
        held[0].continue_()
    assert not (set(names) & set(store.genres.get_genre_whitelist()))
    for name in names:
        expect(_chip(page, name)).to_have_count(0)


def test_queued_add_then_remove_keeps_the_last_explicit_edit(live_app, page, store):
    name = 'Ordered custom genre'
    store.genres.add_genre(name)
    _open_editor(live_app, page)
    search = page.get_by_role('searchbox', name='Find a genre, alias or family')
    with _completed_request(page, '/genres/list'):
        search.fill('Ordered')
    held = []
    page.route('**/genres/add', lambda route: held.append(route))
    page.get_by_role('textbox', name='Genre tag', exact=True).fill(name)
    with page.expect_request('**/genres/add'):
        page.get_by_role('button', name='Add', exact=True).click()
    page.get_by_role('button', name=f'Remove {name}', exact=True).click()
    with _completed_request(page, '/genres/list'):
        search.fill('Ordered custom')
        held[0].continue_()
    assert name not in store.genres.get_genre_whitelist()
    expect(_chip(page, name)).to_have_count(0)


def test_failed_add_keeps_draft_and_does_not_block_later_edits(live_app, page, store):
    _open_editor(live_app, page)
    held = []

    def hold_first(route):
        if not held:
            held.append(route)
        else:
            route.continue_()

    page.route('**/genres/add', hold_first)
    field = page.get_by_role('textbox', name='Genre tag', exact=True)
    field.fill('   ')
    with page.expect_request('**/genres/add'):
        field.press('Enter')
    name = 'After invalid add'
    field.fill(name)
    field.press('Enter')
    field.fill('Unsubmitted draft')
    with _completed_request(page, '/genres/list'):
        page.get_by_role('searchbox', name='Find a genre, alias or family').fill(name)
        held[0].continue_()
    assert name in store.genres.get_genre_whitelist()
    expect(_chip(page, name)).to_be_visible()
    expect(field).to_have_value('Unsubmitted draft')
    expect(page.get_by_text('enter a genre name', exact=True)).to_be_visible()
