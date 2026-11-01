"""Render pinned, read-only account examples through the production partial.

Run via ``./experiments/run.py evidence-audit browser`` after ``snapshot``.
These component checks supplement the isolated full-app browser journey.
"""
import html
import json
import platform

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import expect

from experiments.audio_lab_tools.evidence_audit import OUTPUT, ROOT
from experiments.audio_lab_tools.provider_pilot import digest, save
from yt_playlist.web.musical_evidence import describe


@pytest.mark.browser
@pytest.mark.parametrize('width', [1280, 390])
def test_account_provenance_examples(page, width):
    snapshot = OUTPUT / 'snapshot.json'
    assert snapshot.exists(), 'Run ./experiments/run.py evidence-audit snapshot first'
    report = json.loads(snapshot.read_bytes())
    required = {'vibe:audio_measurement', 'vibe:provider_measurement', 'vibe:artist_genre',
                'vibe:artist', 'vibe:genre', 'vibe:heuristic', 'explicit_correction',
                'conflicting_sources', 'sparse_attributes'}
    assert required <= report['cases'].keys()
    templates = ROOT / 'src/yt_playlist/web/templates'
    static = ROOT / 'src/yt_playlist/web/static'
    environment = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    template = environment.get_template('_partials/track_musical_evidence.html')
    css = '\n'.join((static / name).read_text() for name in ('tokens.css', 'app.css', 'track-detail.css'))
    page.set_viewport_size({'width': width, 'height': 900})
    observed = []
    for flag in sorted(required):
        case = report['cases'][flag]
        musical = describe(case['profile'], case['sources'], truncated=case['truncated'])
        body = template.render(musical=musical)
        document = ('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<style>{css}</style><main class="track-detail" style="padding:24px">'
                    f'<h1>{html.escape(case["title"])}</h1><p>{html.escape(case["artist"])}</p>{body}</main>')
        page.set_content(document)
        evidence = page.locator('.track-musical-evidence')
        evidence.locator('> summary').focus()
        page.keyboard.press('Enter')
        expect(evidence).to_have_attribute('open', '')
        vibes = evidence.locator('.track-vibe-evidence')
        vibes.locator('> summary').focus()
        page.keyboard.press('Enter')
        expect(vibes).to_have_attribute('open', '')
        expect(vibes.locator('[data-vibe-evidence]')).to_have_count(6)
        expect(vibes).to_contain_text('not your ratings')
        if flag.startswith('vibe:'):
            source = flag.split(':', 1)[1]
            names = [name for name, fit in case['profile']['vibes']['fits'].items() if fit['source'] == source]
            for row in musical['vibes']:
                if row['name'] in names:
                    expect(vibes.locator(f'[data-vibe-evidence="{row["name"]}"]')).to_contain_text(row['basis'])
        if flag == 'explicit_correction':
            expect(evidence).to_contain_text('Your correction')
        if flag == 'conflicting_sources':
            expect(evidence).to_contain_text('Sources disagree.')
        if flag == 'sparse_attributes':
            expect(evidence.locator('> .track-musical-signals')).to_contain_text('Unknown')
        versions = vibes.get_by_text('Estimate versions', exact=True)
        if versions.count():
            versions.click()
            expect(vibes).to_contain_text(case['profile']['vibes']['inputs_version'])
        sources = evidence.locator('.track-musical-sources > summary')
        if sources.count():
            sources.click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        slug = flag.replace(':', '-')
        screenshot = OUTPUT / f'{slug}-{width}.png'
        page.screenshot(path=str(screenshot), full_page=True)
        observed.append({'case': flag, 'key': case['key'], 'screenshot': screenshot.name,
                         'screenshot_sha256': digest(screenshot), 'rows': musical['vibes']})
    save(OUTPUT / f'browser-{width}.json', {'snapshot_sha256': digest(snapshot),
         'python': platform.python_version(), 'browser': page.context.browser.version,
         'width': width, 'observed': observed})
