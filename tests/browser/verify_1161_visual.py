"""Disposable visual acceptance check for the annotated #1161 revision."""
import io
import json

import pytest
from PIL import Image
from playwright.sync_api import expect

from yt_playlist.rec import account_growth, festival_layout


@pytest.mark.browser
def test_annotated_paths(live_app, store, page, monkeypatch):
    actual_view = account_growth.view

    def opened(source, now, **kwargs):
        state = actual_view(source, now, **kwargs)
        for node in state['nodes']:
            node.update(state='explored', ready=True, upcoming=False)
        state['trails'] = festival_layout.trails(state['nodes'])
        return state

    monkeypatch.setattr(account_growth, 'view', opened)
    store.settings.set_setting('onboard_dismissed', '1')
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('**/radio/seeds', lambda route: route.fulfill(json={'tracks': [], 'menu_id': ''}))
    page.set_viewport_size({'width': 3542, 'height': 2400})
    page.goto(live_app + '/#unlocks')
    svg = page.locator('.festival-map')
    routes = page.locator('.festival-routes')
    expect(routes.locator('.growth-route:visible')).to_have_count(19)
    page.mouse.move(0, 0)
    page.evaluate('''() => {
        Alpine.$data(document.querySelector('.home-growth')).refresh = async () => {};
        document.querySelectorAll('.growth-unlock-notice,.growth-home-choice').forEach(el => el.style.setProperty('display','none','important'));
    }''')
    svg.screenshot(path='/tmp/tuneconsole-1161-full-open.png', animations='disabled')
    routes.locator('.growth-route').evaluate_all("els => els.forEach(el => el.setAttribute('class','growth-route is-preparing'))")
    svg.screenshot(path='/tmp/tuneconsole-1161-full-preparing.png', animations='disabled')
    geometry = routes.locator('path').evaluate_all('''paths => {
        const endpoint = (path, s) => path.getPointAtLength(s);
        const forks = [[1195,330],[1287,245],[620,852]].map(([x,y]) =>
            paths.filter(p => [0,p.getTotalLength()].some(s => {
                const q = endpoint(p,s); return Math.hypot(q.x-x,q.y-y) < 2;
            })).length);
        const points = paths.map(p => {
            const result = [];
            for (let s = 16; s < p.getTotalLength()-16; s += 4) {
                const a=p.getPointAtLength(s), b=p.getPointAtLength(s+2), n=Math.hypot(b.x-a.x,b.y-a.y);
                result.push({x:a.x,y:a.y,dx:(b.x-a.x)/n,dy:(b.y-a.y)/n});
            }
            return result;
        });
        const repeats=[];
        for (let i=0;i<paths.length;i++) for (let j=i+1;j<paths.length;j++) {
            const overlap=points[i].filter(a => points[j].some(b =>
                Math.hypot(a.x-b.x,a.y-b.y)<7 && Math.abs(a.dx*b.dx+a.dy*b.dy)>.95));
            if (overlap.length>=6) repeats.push([i,j,overlap.length]);
        }
        return {forks,repeats};
    }''')
    assert geometry == {'forks': [3, 3, 3], 'repeats': []}, geometry
    page.set_viewport_size({'width': 1700, 'height': 1400})
    page.locator('.landmark-art').evaluate_all("els => els.forEach(el => el.style.visibility='hidden')")

    def snapshot():
        return Image.open(io.BytesIO(svg.screenshot(animations='disabled'))).convert('RGB')

    routes.evaluate("el => el.style.visibility='hidden'")
    dark = snapshot()
    routes.evaluate("el => el.style.visibility='visible'")
    preparing = snapshot()
    routes.evaluate("el => el.removeAttribute('mask')")
    unmasked = snapshot()
    routes.evaluate("el => el.setAttribute('mask','url(#festival-path-surface)')")
    routes.locator('.growth-route').evaluate_all("els => els.forEach(el => el.setAttribute('class','growth-route is-explored'))")
    opened = snapshot()
    for name, shot in [('dark', dark), ('preparing', preparing), ('unmasked', unmasked), ('open', opened)]:
        shot.save('/tmp/tuneconsole-1161-pixels-'+name+'.png')

    def delta(image, point, radius=0):
        px, py = round(point[0]*dark.width/1536), round(point[1]*dark.height/1024)
        return max(abs(a-b) for x in range(px-radius,px+radius+1)
                   for y in range(py-radius,py+radius+1)
                   for a,b in zip(dark.getpixel((x,y)),image.getpixel((x,y))))

    foreground = {'entrance fir': (136,503), 'fork lamp': (350,534),
                  'forecourt lamp': (427,505), 'camper roof': (1008,311),
                  'camper left lamp': (911,365), 'camper projecting shelf': (915,377),
                  'low forecourt fence': (472,511), 'lodge front lamp': (790,239),
                  'gateway post': (1194,316), 'Revisit stair lamp': (1332,263),
                  'lodge flowers': (1004,217), 'bridge handrail': (1335,374),
                  'bridge far rail': (1350,362), 'bridge first lamp': (1329,354),
                  'bridge middle lamp': (1370,372), 'bridge last lamp': (1422,405),
                  'stage visitor': (1246,623), 'island post': (1002,645),
                  'island rail': (1049,663), 'cinema screen': (1100,780)}
    evidence = {name: [delta(unmasked,p),delta(preparing,p),delta(opened,p)]
                for name,p in foreground.items()}
    assert all(a>4 and b<=2 and c<=4 for a,b,c in evidence.values()), json.dumps(evidence)
    for point in [(424,455),(429,480),(550,525),(750,245),(775,237),(820,224),(1316,252),
                  (966,199),(1015,214),(1195,330),(1217,282),(1275,584),
                  (909,397),(930,414),(1040,646),(1080,674),(1340,375),
                  (1390,400),(620,852),(734,402),(761,446),(861,670),(640,700)]:
        assert delta(opened,point,5)>4, f'Open trail misses the illustrated walking surface at {point}'
        assert delta(preparing,point,3)>4, f'Preparing trail misses the illustrated walking surface at {point}'
    for point in [(1100,330),(1284,573),(875,689),(920,688),(500,910),(310,850)]:
        assert delta(opened,point)<=4 and delta(preparing,point)<=2, f'Painted foreground/bank at {point}'
