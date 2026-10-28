"""Prepared analytics routes and the browser worker's filtering contract."""
import pathlib
import subprocess

from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from yt_playlist.rec.choice_capture import CaptureQueue
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo


@pytest.fixture(autouse=True)
def listening_enabled(monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS', '1')


@pytest.mark.parametrize('path',['/charts?view=analytics&window=7d','/charts','/charts?view=home'])
def test_listening_pages_do_not_scan_legacy_charts(store, monkeypatch,path):
    def forbidden(*args, **kwargs):
        raise AssertionError('analytics requested foreground history aggregation')
    monkeypatch.setattr(store.charts, 'history_bounds', forbidden)
    monkeypatch.setattr(store.charts, 'top_tracks', forbidden)
    import json
    ListeningAnalyticsRepo(store).publish(json.dumps({'available':True,'id':'edition','stories':[]}),1,'listening_edition')
    with TestClient(create_app(store, lambda: {})) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert 'listeningAnalytics(\'charts\',' in response.text
        assert 'listening-analytics.js' in response.text


def test_reporting_timezone_is_validated_and_scheduled(store,monkeypatch):
    calls=[]
    app=create_app(store,lambda:{})
    monkeypatch.setattr(app.state.ctx.rec_worker,'trigger_analytics_refresh',lambda:calls.append(True))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        for value in (None,{},'not/a/timezone','/etc/passwd'):
            assert client.post('/charts/listening-timezone',json={'timezone':value}).status_code==422
        assert not calls
        assert client.post('/charts/listening-timezone',json={'timezone':'America/Costa_Rica'}).status_code==202
        assert store.settings.get_setting('listening_timezone')=='America/Costa_Rica'
        assert calls==[True]


def test_editorial_exposure_is_queued_only_for_current_edition_stories(store):
    import json
    offered=[]
    store.choice_capture=CaptureQueue(lambda capture:offered.append(capture) or True)
    ListeningAnalyticsRepo(store).publish(json.dumps({'id':'current','stories':[{'id':'lead'},{'id':'support'}]}),1,'listening_edition')
    with TestClient(create_app(store,lambda:{}),base_url='http://127.0.0.1') as client:
        assert client.post('/charts/listening-impressions',json={'edition':'old','stories':['lead']}).status_code==204
        assert client.post('/charts/listening-impressions',json={'edition':'current','stories':['unknown']}).status_code==204
        assert not offered
        assert client.post('/charts/listening-impressions',json={'edition':'current','stories':['lead','support','unknown']}).status_code==204
        assert len(offered)==1
        assert set(offered[0].args[0])=={'lead','hero:lead','support'}
        assert store.conn.execute("SELECT COUNT(*) FROM rec_impressions WHERE surface='listening_editorial'").fetchone()[0]==0
        offered[0].run()
        assert store.conn.execute("SELECT COUNT(*) FROM rec_impressions WHERE surface='listening_editorial'").fetchone()[0]==3
        assert client.post('/charts/listening-impressions',json={'edition':'current','stories':[1]}).status_code==400


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
assert.equal(seven.total.hearts,0); assert.equal(seven.total.skulls,1);
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
assert.equal(format.total.plays,4); assert.equal(format.total.hearts,0);
assert.equal(format.total.skulls,0); // confirmed actions have unknown playback format
assert.equal(summarize(data,{...filters,window:'all'}).total.plays,19);
const weeks = summarize(data,{...filters,period:'week'});
assert.equal(weeks.series[0].day,'2026-10-12');
assert.equal(weeks.series[0].plays,4); assert.equal(weeks.series[1].plays,2);
const none = summarize(data,{...filters,artist:'missing'});
assert.equal(none.total.plays,0); assert.equal(none.series.length,7);
const long = summarize({...data,rows:[['2000-01-01','A','Techno','mixes',1,0,1,1,1,0,0,0,0,0]]}, {...filters,window:'all',period:'auto'});
assert.equal(long.period,'month'); assert.equal(long.total.plays,1);
const explicit = summarize({...data,rows:[['2000-01-01','A','Techno','mixes',1,0,1,1,1,0,0,0,0,0]]}, {...filters,window:'all',period:'day'});
assert.equal(explicit.period,'day');
assert.throws(()=>summarize({...data,rows:[['1900-01-01','A','Techno','mixes',1,0,1,1,1,0,0,0,0,0]]}, {...filters,window:'all',period:'day'}),/shorter range/);
const multi=summarize(data,{...filters,artist:['A','B'],genre:['Jazz','Techno']});
assert.equal(multi.total.plays,6);
const trackColumns=[...columns,'track'];
const recordings={...data,columns:trackColumns,rows:data.rows.map((row,i)=>[...row,'track-'+i])};
assert.equal(summarize(recordings,{...filters,track:['track-1','track-2']}).total.plays,4);
const unknown={...data,rows:[['2026-10-24','','','other',1,0,0,0,0,0,0,0,0,0]]};
assert.equal(summarize(unknown,{...filters,artist:['@unknown']}).total.plays,1);
assert.equal(summarize(unknown,filters).options.artists[0].id,'@unknown');
const custom=summarize(data,{...filters,window:'custom',start:'2026-10-17',end:'2026-10-24'});
assert.equal(custom.total.plays,7); assert.equal(custom.series.length,7);
assert.throws(()=>summarize(data,{...filters,window:'custom',start:'2026-02-30',end:'2026-10-24'}),/valid dates/);
assert.throws(()=>summarize(data,{...filters,window:'custom',start:'2026-10-24',end:'2026-10-17'}),/end date after/);
assert.equal(custom.modules.feedback,true);
assert.equal(imported.modules.discovery,false); assert.equal(imported.modules.formats,false);
""", str(worker)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_subject_drillthrough_preserves_the_analysis_selection():
    component=pathlib.Path(__file__).parents[1]/'src/yt_playlist/web/static/listening-analytics.js'
    result=subprocess.run(['node','-e',r"""
const assert=require('node:assert/strict');
global.window={}; global.document={currentScript:{src:'http://localhost/static/listening-analytics.js'}};
require(process.argv[1]);
const view=window.listeningAnalytics('charts');
Object.assign(view,{genre:['Jazz','Techno'],format:['albums'],track:['one','two'],measure:'minutes',compare:true,
    result:{start:'2026-10-01',end:'2026-10-24'}});
const url=new URL(view.artistURL('42'),'http://localhost');
assert.equal(url.searchParams.get('artist'),'42');
assert.deepEqual(url.searchParams.getAll('genre'),['Jazz','Techno']);
assert.deepEqual(url.searchParams.getAll('track'),['one','two']);
assert.equal(url.searchParams.get('format'),'albums');
assert.equal(url.searchParams.get('measure'),'minutes');
assert.equal(url.searchParams.get('compare'),'true');
assert.equal(url.searchParams.get('start'),'2026-10-01');
assert.equal(url.searchParams.get('end'),'2026-10-24');
""",str(component)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_discovery_excludes_capture_boundary_and_requires_recent_repeat_listening():
    worker = pathlib.Path(__file__).parents[1] / 'src/yt_playlist/web/static/listening-analytics-worker.js'
    result = subprocess.run(['node', '-e', r"""
const assert=require('node:assert/strict');
const {summarize}=require(process.argv[1]);
const columns=['day','track','plays','new_tracks','new_artists','artist_plays'];
const rows=Array.from({length:5},(_,i)=>[`2026-10-${20+i}`,'new-'+i,20,20,20,20]);
const data={today:'2026-10-24',columns,rows,catalog:{}};
const filters={window:'all',period:'day'};
const fresh=summarize(data,filters);
assert.equal(fresh.total.plays,100);
assert.equal(fresh.modules.activity,true);
assert.equal(fresh.modules.discovery,false); // 100% at initial capture is never discovery
const qualified={...data,discovery_from:'2026-10-20'};
assert.equal(summarize(qualified,filters).modules.discovery,false); // baseline alone is insufficient
qualified.catalog={old:{cohort:{first:'2026-08-01',age:84,days14:0,days28:0}}};
qualified.rows=[['2026-10-01','initial',100,100,100,100],...rows,
 ...[20,21,22].map(day=>[`2026-10-${day}`,'old',10,0,0,10])];
const established=summarize(qualified,filters);
assert.equal(established.modules.discovery,true);
assert.equal(established.total.plays,230); // activity retains the whole selected record
assert.equal(established.discovery.total.plays,130);
assert.equal(established.discovery.total.new_tracks,100); // rate excludes initial capture
assert.equal(established.discovery.series[0].plays,0);
const captureOnly=summarize(qualified,{...filters,window:'custom',start:'2026-10-01',end:'2026-10-20'});
assert.equal(captureOnly.modules.activity,true);
assert.equal(captureOnly.modules.discovery,false);
""", str(worker)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
