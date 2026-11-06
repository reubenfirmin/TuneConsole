"""Persistent Chromium must work with the project's long fixture paths."""
import os


def test_persistent_chromium_socket_stays_in_owned_fixture_storage(
        playwright, browser_type_launch_args, tmp_path, stdlib_test_storage):
    profile = tmp_path / 'profile'
    with playwright.chromium.launch_persistent_context(
            profile, **{**browser_type_launch_args, 'channel': 'chromium'},
            args=['--disable-audio-output']) as context:
        page = context.pages[0]
        page.set_content('<title>Private persistent Chromium</title>')
        assert page.title() == 'Private persistent Chromium'
        socket = (profile / 'SingletonSocket').readlink()
        assert socket.exists()
        assert len(os.fsencode(socket)) < 108
        assert socket.resolve().is_relative_to(stdlib_test_storage)
