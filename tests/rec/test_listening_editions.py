"""Editorial claims, stable identity, daily selection and honest sparse states."""
from datetime import datetime, timezone

from yt_playlist.rec import listening_editions, listening_insights
from yt_playlist.rec.listening_analytics import FIELDS
from yt_playlist.rec.listening_dates import edition_day

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()
COLUMNS = ['day','artist','genre','format',*FIELDS,'track','artist_id']


class Repo:
    def __init__(self):
        self.edition = None
        self.views = {}

    def prepared(self, surface):
        return self.edition

    def impressions(self):
        return self.views


def data():
    return {'today':'2026-10-24','timezone':'UTC','columns':COLUMNS,'rows':[],
            'catalog':{},'first_seen':{},'subjects':{}}


def add(payload, key, artist, day, plays=1, **metrics):
    payload['catalog'][key] = {'identity_key':key,'title':key.title(),'artist':artist,
                             'artist_id':artist,'genre':'Jazz','thumbnail':None}
    row = {'day':day,'artist':artist,'genre':'Jazz','format':'albums',
           **dict.fromkeys(FIELDS,0),'plays':plays,'track':key,'artist_id':artist,**metrics}
    payload['rows'].append([row[c] for c in COLUMNS])
    payload['first_seen'][key] = min(day,payload['first_seen'].get(key,day))


def mature():
    payload=data()
    add(payload,'baseline','Earlier','2026-06-01')
    for artist in ('Ada','Bea','Cleo'):
        for day in ('2026-10-19','2026-10-21','2026-10-23'):
            add(payload,artist.lower(),artist,day,3)
    return payload


def test_empty_or_one_imported_day_does_not_earn_a_home_tab():
    payload=data()
    assert listening_editions.prepare(payload,Repo(),NOW)['available'] is False
    add(payload,'sparse','New','2026-10-24',plays=0,sightings=1)
    assert listening_insights.candidates(payload) == []


def test_attachment_needs_independent_days_and_survives_more_plays_with_same_identity():
    payload=mature()
    items=[c for c in listening_insights.candidates(payload) if c['family']=='attachment']
    assert len(items)==3
    before=next(s for s in items if s['subject']=='artist:Ada')
    add(payload,'ada','Ada','2026-10-24',20)
    after=next(s for s in listening_insights.candidates(payload) if s['subject']=='artist:Ada' and s['family']=='attachment')
    assert before['id']==after['id']
    assert before['evidence'] != after['evidence']
    assert 'artist=Ada' in after['url'] and 'start=' in after['url'] and 'end=' in after['url']


def test_first_week_of_available_history_is_left_censored():
    payload=data()
    for day in ('2026-10-19','2026-10-21','2026-10-23'):
        add(payload,'a','Ada',day,4)
    assert not any(s['family']=='attachment' for s in listening_insights.candidates(payload))


def test_return_requires_a_gap_and_multiple_recent_and_earlier_days():
    payload=mature()
    for day in ('2026-07-01','2026-07-03'):
        add(payload,'ada','Ada',day)
    returns=[s for s in listening_insights.candidates(payload) if s['family']=='return']
    assert len(returns)==1 and '108 days' in returns[0]['evidence']


def test_artist_return_combines_recordings_and_drills_into_the_whole_artist():
    payload=data()
    for day in ('2026-07-01','2026-07-03'):
        add(payload,'old','Ada',day)
    add(payload,'a','Ada','2026-10-20',3)
    add(payload,'b','Ada','2026-10-22',2)
    add(payload,'a','Ada','2026-10-23',2)
    story=next(s for s in listening_insights.candidates(payload) if s['family']=='return')
    assert story['timeline'][0]['name']=='Ada'
    assert story['timeline'][0]['counts']=={'2026-10-20':3,'2026-10-22':2,'2026-10-23':2}
    assert len(story['timeline'])==1
    assert 'artist=Ada' in story['url'] and 'track=' not in story['url']
    assert story['stat'] == 109
    assert story['stat_label'] == 'days between plays'


def test_orbit_players_use_each_days_actual_song_with_stable_ties():
    payload=data()
    for day in ('2026-07-01','2026-07-03'):
        add(payload,'old','Ada',day)
    add(payload,'a','Ada','2026-10-20',2)
    add(payload,'b','Ada','2026-10-20',1)
    add(payload,'b','Ada','2026-10-20',2,format='radio')
    add(payload,'a','Ada','2026-10-22')
    add(payload,'unplayable','Ada','2026-10-22')
    add(payload,'a','Ada','2026-10-23',2)
    add(payload,'a','Ada','2026-10-21',plays=0,sightings=1)
    for key in ('old','a','b'):
        payload['catalog'][key].update(video_id='video-'+key,thumbnail='https://example.com/'+key+'.jpg')

    def players():
        story=next(s for s in listening_insights.candidates(payload) if s['family']=='return')
        return story['timeline'][0]['songs']

    songs=players()
    assert set(songs)=={'2026-10-20','2026-10-22','2026-10-23'}
    assert songs['2026-10-20']=={
        'id':'b','name':'B','artist':'Ada','video_id':'video-b','thumbnail':'https://example.com/b.jpg'}
    assert songs['2026-10-22']['id']=='a'
    payload['rows'].reverse()
    assert players()==songs


def test_a_few_old_plays_do_not_turn_a_long_gap_into_a_comeback_story():
    payload=data()
    for day in ('2026-07-01','2026-07-03','2026-10-18','2026-10-19','2026-10-20'):
        add(payload,'a','Ada',day)
    assert not any(s['family']=='return' for s in listening_insights.candidates(payload))


def test_exploring_several_new_songs_beats_a_sparse_return_and_replaces_its_held_lead():
    payload=data()
    for day in ('2026-07-01','2026-07-03','2026-10-18','2026-10-19','2026-10-20'):
        add(payload,'old-return','Sparse return',day,3)
    repo=Repo()
    repo.edition=listening_editions.prepare(payload,repo,NOW)
    assert repo.edition['stories'][0]['family']=='return'
    repo.edition['version']=listening_editions.VERSION-1
    for row in payload['rows']:
        if row[0]>='2026-10-01':
            row[COLUMNS.index('plays')]=1
    add(payload,'known','Exploring','2026-06-01')
    add(payload,'known','Exploring','2026-10-23')
    for index in range(7):
        add(payload,'new-'+str(index),'Exploring','2026-10-'+str(20+index%4))
    edition=listening_editions.prepare(payload,repo,NOW)
    assert edition['stories'][0]['family']=='deeper'
    assert edition['stories'][0]['subject']=='artist:Exploring'
    assert '7 more' in edition['stories'][0]['evidence']
    assert {row['id'] for row in edition['stories'][0]['timeline']}=={'new-'+str(i) for i in range(7)}
    assert edition['stories'][0]['url'].count('track=new-')==7


def test_freshness_uses_actual_activity_not_the_end_of_the_drawing():
    payload=mature()
    before=next(s for s in listening_insights.candidates(payload) if s['subject']=='artist:Ada')
    payload['today']='2026-10-28'
    after=next(s for s in listening_insights.candidates(payload) if s['subject']=='artist:Ada')
    assert before['timeliness']==1
    assert after['timeliness']==.2
    add(payload,'ada','Ada','2026-10-28',plays=0,hearts=1)
    after_heart=next(s for s in listening_insights.candidates(payload) if s['subject']=='artist:Ada')
    assert after_heart['timeliness']==.2


def test_artwork_is_selective_stable_and_can_move_between_stories():
    stories=[{'id':str(i),'art':f'/cover-{i}.jpg','timeline':[]} for i in range(4)]
    edition={'day':'2026-10-24','stories':stories}
    first=listening_editions.present(edition)
    assert sum(bool(s['backdrop']) for s in first['stories'])==2
    assert listening_editions.present(edition)==first
    placements=set()
    for day in range(10,25):
        reading=listening_editions.present({**edition,'day':f'2026-10-{day}'})
        placements.add(tuple(s['id'] for s in reading['stories'] if s['backdrop']))
    assert len(placements)>1
    assert all('backdrop' not in story for story in stories)


def test_same_day_reload_holds_edition_and_next_day_can_lead_with_unseen_subject():
    payload=mature(); repo=Repo()
    first=listening_editions.prepare(payload,repo,NOW)
    assert len(first['stories'])==1  # another artist is still the same theme
    repo.edition=first
    hero=first['stories'][0]
    repo.views={hero['id']:NOW,'hero:'+hero['id']:NOW}
    assert listening_editions.prepare(payload,repo,NOW+3600)==first
    tomorrow=listening_editions.prepare(payload,repo,NOW+86400)
    assert tomorrow['stories'] == first['stories']


def test_selection_keeps_distinct_families_without_filling_with_another_artist():
    payload=mature()
    for row in payload['rows']:
        if row[0]>='2026-10-01':
            row[COLUMNS.index('seconds')]=1200
    edition=listening_editions.prepare(payload,Repo(),NOW)
    families=[s['family'] for s in edition['stories']]
    assert len(families)==2
    assert set(families[:2])=={'attachment','formats'}
    assert families.count('attachment')==1
    assert len({s['subject'] for s in edition['stories']})==2


def test_selector_upgrade_keeps_held_lead_without_repeating_the_family():
    payload=mature(); repo=Repo()
    first=listening_editions.prepare(payload,repo,NOW)
    held=first['stories'][0]
    repo.edition={**first,'version':listening_editions.VERSION-1,'stories':[held]}
    repo.views={held['id']:NOW,'hero:'+held['id']:NOW}
    refreshed=listening_editions.prepare(payload,repo,NOW+60)
    assert refreshed['stories'][0]['id']==held['id']
    assert len(refreshed['stories'])==1
    repo.edition=refreshed
    assert listening_editions.prepare(payload,repo,NOW+120)==refreshed


def test_no_distinct_eligible_story_retains_the_current_reading():
    payload=mature(); repo=Repo()
    payload['rows']=[r for r in payload['rows'] if r[COLUMNS.index('artist')] in ('Earlier','Ada')]
    first=listening_editions.prepare(payload,repo,NOW); repo.edition=first
    hero=first['stories'][0]; repo.views={'hero:'+hero['id']:NOW,hero['id']:NOW}
    tomorrow=listening_editions.prepare(payload,repo,NOW+86400)
    assert tomorrow['stories']==first['stories']


def test_new_presentation_replaces_stored_copy_without_changing_story_identity():
    payload=mature(); repo=Repo()
    first=listening_editions.prepare(payload,repo,NOW)
    repo.edition={**first,'version':listening_editions.VERSION-1,
                  'stories':[{**s,'title':'Old wording'} for s in first['stories']]}
    refreshed=listening_editions.prepare(payload,repo,NOW+60)
    assert refreshed['version']==listening_editions.VERSION
    assert refreshed['stories'][0]['id']==first['stories'][0]['id']
    assert refreshed['stories'][0]['title']!='Old wording'
    assert refreshed['stories'][0]['timeline'][0]['songs']


def test_imported_daily_entries_do_not_turn_into_plays_in_editorial_drawings():
    payload=mature()
    add(payload,'ada','Ada','2026-10-20',plays=0,sightings=1)
    story=next(s for s in listening_insights.candidates(payload) if s['subject']=='artist:Ada')
    assert '2026-10-20' not in story['timeline'][0]['counts']
    assert '2026-10-20' not in story['timeline'][0]['songs']


def test_missing_artist_and_one_ordinary_rating_do_not_create_a_feature():
    payload=data(); add(payload,'a','','2026-10-23',plays=0,hearts=1)
    items=listening_insights.candidates(payload)
    assert items == []
    assert not listening_editions.prepare(payload, Repo(), NOW)['available']


def test_editorial_day_rolls_at_six_in_the_reporting_timezone():
    before=datetime(2026,10,24,11,59,tzinfo=timezone.utc).timestamp()
    after=datetime(2026,10,24,12,0,tzinfo=timezone.utc).timestamp()
    assert edition_day(before,'America/Costa_Rica')=='2026-10-23'
    assert edition_day(after,'America/Costa_Rica')=='2026-10-24'


def test_completed_week_format_and_genre_shifts_have_real_denominators():
    payload=data(); add(payload,'baseline','Earlier','2026-06-01')
    for day in ('2026-10-05','2026-10-07','2026-10-09'):
        add(payload,'older','Ada',day,10,seconds=1200,format='albums',genre='Jazz')
    for day in ('2026-10-12','2026-10-14','2026-10-16'):
        add(payload,'newer','Bea',day,10,seconds=1200,format='radio',genre='Techno')
    items=listening_insights.candidates(payload)
    formats=next(s for s in items if s['family']=='format_shift')
    genres=next(s for s in items if s['family']=='genre_shift')
    assert '100% to 0%' not in formats['evidence']
    assert '0% to 100%' in formats['evidence']
    assert '0% to 100%' in genres['evidence']
    assert 'format=' not in formats['url']  # the comparison retains the full denominator
    assert 'genre=' not in genres['url']
    assert 'track=' not in formats['url'] and 'track=' not in genres['url']
    assert {d['day'] for d in formats['band_days']}=={'2026-10-05','2026-10-07','2026-10-09','2026-10-12','2026-10-14','2026-10-16'}
    assert formats['since']=='2026-10-05' and formats['until']=='2026-10-19'
    for row in payload['rows']:
        if row[0]<'2026-10-12':
            row[COLUMNS.index('genre')]=''
    assert not any(s['family']=='genre_shift' for s in listening_insights.candidates(payload))


def test_first_week_and_zero_baseline_cannot_be_described_as_growth():
    payload=data(); add(payload,'baseline','Earlier','2026-06-01')
    for day in ('2026-10-20','2026-10-22','2026-10-24'):
        add(payload,'new','Ada',day,10)
    assert not any(s['family']=='rising' for s in listening_insights.candidates(payload))


def test_rising_compares_completed_days_and_keeps_the_artist_denominator():
    payload=data(); add(payload,'baseline','Earlier','2026-06-01')
    add(payload,'a','Ada','2026-10-12',3)
    for day in ('2026-10-18','2026-10-20','2026-10-22'):
        add(payload,'a','Ada',day,4)
    add(payload,'a','Ada','2026-10-24',100)
    story=next(s for s in listening_insights.candidates(payload) if s['family']=='rising')
    assert '12 plays' in story['evidence'] and 'against 3' in story['evidence']
    assert story['since']=='2026-10-10' and story['until']=='2026-10-24'
    assert 'track=' not in story['url']
