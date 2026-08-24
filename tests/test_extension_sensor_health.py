"""The extension must distinguish a connected service worker from a responding YTM tab sensor."""
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_content_script_answers_explicit_sensor_ping():
    src = (ROOT / "extension/content.js").read_text()
    assert 'msg.type === "sensor-ping"' in src
    assert "sendResponse({ ok: true" in src


def test_background_probe_reinjects_and_reports_health():
    src = (ROOT / "extension/background.js").read_text()
    probe = src[src.index("async function probeSensorHealth"):]
    assert "await pingSensor(tab.id)" in probe
    assert "await inject(tab.id)" in probe
    assert 'type: "sensor-health"' in probe
    assert "respondingTabs" in probe
    assert "probeSensorHealth();" in src[src.index("chrome.alarms.onAlarm"):]


def test_injection_failure_is_not_silently_swallowed():
    src = (ROOT / "extension/background.js").read_text()
    inject = src[src.index("async function inject"):src.index("async function injectAllYtmTabs")]
    assert "console.warn" in inject
    assert "return { ok: false, error }" in inject
