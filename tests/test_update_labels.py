from yt_playlist.web.update_labels import update_instruction


def test_update_instruction_per_kind():
    assert update_instruction("flatpak") == ("Update now", None)
    assert update_instruction("macos") == ("Update now", None)
    assert update_instruction("release") == (
        "Get the latest release", "https://github.com/reubenfirmin/TuneConsole/releases/latest")


def test_update_instruction_unknown_kind_falls_back_to_release():
    assert update_instruction("something-unrecognized") == update_instruction("release")
