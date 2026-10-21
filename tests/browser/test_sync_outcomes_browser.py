"""The live console must not announce a complete refresh for a partial or failed sync."""
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome", ["complete", "partial", "skipped", "failed"])
def test_console_respects_terminal_outcome(page, outcome):
    page.goto("about:blank")
    page.add_script_tag(path=str(Path(__file__).parents[2] /
                                "src/yt_playlist/web/static/app.js"))
    result = page.evaluate("""outcome => {
        const sources = [];
        window.EventSource = class {
            constructor() { sources.push(this); }
            close() { this.closed = true; }
        };
        const timers = [];
        window.setTimeout = (callback, delay) => timers.push(delay);
        const console = homeStatus();
        console.$nextTick = callback => callback();
        console.$refs = {};
        console.attach(1);
        sources[0].onmessage({data: JSON.stringify({type: 'end', outcome,
            error: outcome === 'complete' ? null : 'Incomplete sync'})});
        return {running: console.running, failed: console.failed, closed: sources[0].closed,
            lines: console.lines, timers};
    }""", outcome)
    assert not result["running"] and result["closed"]
    text = " ".join(line["text"] for line in result["lines"])
    if outcome == "complete":
        assert "library updated" in text
        assert result["timers"] == [900]
        assert not result["failed"]
    else:
        assert "library updated" not in text
        assert result["failed"]
        assert result["timers"] == []
        assert {"partial": "partially updated", "skipped": "skipped", "failed": "failed"}[outcome] in text
