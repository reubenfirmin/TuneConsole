"""Prepared analytics routes and the browser worker's filtering contract."""
import pathlib
import subprocess

from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app


def test_analytics_page_does_not_scan_legacy_charts(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('analytics requested foreground history aggregation')
    monkeypatch.setattr(store.charts, 'history_bounds', forbidden)
    monkeypatch.setattr(store.charts, 'top_tracks', forbidden)
    with TestClient(create_app(store, lambda: {})) as client:
        response = client.get('/charts?view=analytics&window=7d')
        assert response.status_code == 200
        assert 'listeningAnalytics(\'charts\', \'7d\')' in response.text
        assert 'listening-analytics.js' in response.text


def test_worker_filtering_and_calendar_boundaries():
    worker = pathlib.Path(__file__).parents[1] / 'src/yt_playlist/web/static/listening-analytics-worker.js'
    result = subprocess.run(['node', '-e', r"""
const assert = require('node:assert/strict');
const {summarize} = require(process.argv[1]);
const columns = ['day','artist','genre','format','plays','sightings','new_tracks','new_artists','artist_plays','liked','rated','seconds','hearts','skulls'];
const data = {schema:1,today:'2026-10-24',built_at:10,columns,rows:[
 ['2026-09-01','A','Techno','mixes',10,2,1,1,10,0,0,300,4,0],
 ['2026-10-17','A','Techno','mixes',3,0,0,0,3,1,2,60,0,0],
 ['2026-10-18','A','Techno','mixes',4,0,0,0,4,2,4,90,0,0],
 ['2026-10-24','B','Jazz','albums',2,1,1,1,2,0,1,30,0,1],
 ['2026-10-25','B','Jazz','albums',100,0,0,0,100,0,0,100,0,0]]};
const filters = {window:'7d',period:'day',artist:'',genre:'',format:'',overlay:'artist'};
const seven = summarize(data,filters);
assert.equal(seven.total.plays,6); assert.equal(seven.total.seconds,120);
assert.equal(seven.series.length,7); assert.equal(seven.series[0].day,'2026-10-18');
assert.equal(seven.retained.hearts,4); assert.equal(seven.retained.skulls,1);
assert.deepEqual(seven.overlays.map(x=>x.name),['A','B']);
assert.equal(seven.overlays[0].count,4);
assert.equal(seven.series[0].secondsByFormat.mixes,90);
const sightings = summarize(data,{...filters,historyMetric:'sightings'});
assert.equal(sightings.overlays[0].name,'B'); assert.equal(sightings.overlays[0].count,1);
assert.equal(sightings.total.plays,6); // changing chart units never changes discovery denominators
const imported = summarize({...data,rows:[['2026-10-24','Imported','Jazz','other',0,3,0,0,0,0,0,0,0,0]]},filters);
assert.equal(imported.historyMetric,'sightings'); assert.equal(imported.overlays[0].count,3);
assert.equal(summarize(data,{...filters,artist:'A'}).total.plays,4);
assert.equal(summarize(data,{...filters,genre:'Jazz'}).total.plays,2);
const format = summarize(data,{...filters,format:'mixes'});
assert.equal(format.total.plays,4); assert.equal(format.retained.hearts,4);
assert.equal(format.retained.skulls,1); // ratings have no playback-format attribution
assert.equal(summarize(data,{...filters,window:'all'}).total.plays,19);
const weeks = summarize(data,{...filters,period:'week'});
assert.equal(weeks.series[0].day,'2026-10-12');
assert.equal(weeks.series[0].plays,4); assert.equal(weeks.series[1].plays,2);
const none = summarize(data,{...filters,artist:'missing'});
assert.equal(none.total.plays,0); assert.equal(none.series.length,7);
const long = summarize({...data,rows:[['2000-01-01','A','Techno','mixes',1,0,1,1,1,0,0,0,0,0]]}, {...filters,window:'all'});
assert.equal(long.period,'month'); assert.equal(long.total.plays,1);
""", str(worker)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
