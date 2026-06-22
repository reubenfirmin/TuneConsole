from pathlib import Path

PARTIALS = Path("src/yt_playlist/web/templates/_partials")


def test_mood_panels_do_not_claim_a_few_hours_fade():
    for name in ("mood_panel.html", "feedback_panel.html"):
        text = (PARTIALS / name).read_text().lower()
        assert "few hours" not in text and "fades" not in text and "then decays" not in text
        assert "stick" in text or "persist" in text or "until you change" in text
