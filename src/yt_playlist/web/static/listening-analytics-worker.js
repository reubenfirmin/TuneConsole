/* All aggregation, parsing and partition caching stays off the UI thread. */
'use strict';
const metrics = ['plays','sightings','new_tracks','new_artists','artist_plays','liked','rated','seconds','hearts','skulls','recent_discoveries','clears'];
const formats = ['mixes','albums','radio','liked','other'];
const empty = () => Object.fromEntries(metrics.map(k => [k,0]));
const date = day => new Date(day + 'T00:00:00Z');
const stamp = d => d.toISOString().slice(0,10);
const shift = (day,n) => { const d=date(day); d.setUTCDate(d.getUTCDate()+n); return stamp(d); };
const values = value => Array.isArray(value) ? value : value ? [value] : [];
function bucket(day,period) {
    const d=date(day);
    if (period === 'month') d.setUTCDate(1);
    if (period === 'week') d.setUTCDate(d.getUTCDate()-(d.getUTCDay()+6)%7);
    return stamp(d);
}
function range(data,filters) {
    const n={'7d':7,'30d':30,'90d':90}[filters.window];
    const valid = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '') && !isNaN(date(value)) && stamp(date(value))===value;
    if(filters.window === 'custom' && (!valid(filters.start) || !valid(filters.end) || filters.end<=filters.start))
        throw new RangeError('Choose valid dates with an end date after the start date. The end date itself is not included.');
    const start=filters.window === 'custom' && valid(filters.start) ? filters.start : n ? shift(data.today,1-n) : null;
    const end=filters.window === 'custom' && valid(filters.end) ? filters.end : shift(data.today,1);
    return {start,end};
}
function summarize(data,filters) {
    const bounds=range(data,filters), catalog=data.catalog || {};
    const options={artists:new Map(),genres:new Set()}, selected=[];
    const artists=values(filters.artist), genres=values(filters.genre), fmts=values(filters.format);
    const tracks=values(filters.track);
    const total=empty(), previous=empty();
    let earliest=data.today;
    const selectedSpan=bounds.start ? Math.max(1,(date(bounds.end)-date(bounds.start))/86400000) : null;
    const previousStart=selectedSpan ? shift(bounds.start,-selectedSpan) : null;
    for (const row of data.rows) {
        const r=Object.fromEntries(data.columns.map((k,i)=>[k,row[i]]));
        for (const k of metrics) r[k]=Number(r[k]) || 0;
        const aid=String(r.artist_id || r.artist || '@unknown'), name=r.artist || 'Unknown artist';
        options.artists.set(aid,name); options.genres.add(r.genre || 'Unknown');
        if (artists.length && !artists.includes(aid) && !artists.includes(name)) continue;
        if (genres.length && !genres.includes(r.genre || 'Unknown')) continue;
        if (fmts.length && !fmts.includes(r.format)) continue;
        if (tracks.length && !tracks.includes(r.track)) continue;
        if (r.day >= bounds.end || r.day > data.today) continue;
        if (bounds.start && r.day < bounds.start) {
            if (filters.compare && previousStart && r.day >= previousStart) for (const k of metrics) previous[k]+=r[k];
            continue;
        }
        earliest=r.day < earliest ? r.day : earliest;
        selected.push({...r,aid,name});
        for (const k of metrics) total[k]+=r[k];
    }
    const span=Math.max(0,(date(bounds.end)-date(bounds.start || earliest))/86400000);
    const period=['day','week','month'].includes(filters.period) ? filters.period : span>1260 ? 'month' : span>180 ? 'week' : 'day';
    const buckets=new Map(), groups=new Map(), details=new Map();
    const bin = key => ({day:key,...empty(),formats:Object.fromEntries(formats.map(k=>[k,0])),secondsByFormat:Object.fromEntries(formats.map(k=>[k,0]))});
    const historyMetric=filters.historyMetric === 'sightings' || (!total.plays && total.sightings && !filters.historyMetric) ? 'sightings' : 'plays';
    const measure=['minutes','share'].includes(filters.measure) ? filters.measure : 'counts';
    for (const r of selected) {
        const key=bucket(r.day,period);
        if (!buckets.has(key)) buckets.set(key,bin(key));
        const b=buckets.get(key);
        for (const k of metrics) b[k]+=r[k];
        b.formats[r.format]=(b.formats[r.format] || 0)+r.plays;
        b.secondsByFormat[r.format]=(b.secondsByFormat[r.format] || 0)+r.seconds;
        const group=filters.overlay === 'genre' ? r.genre || 'Unknown' : r.aid;
        if (!groups.has(group)) groups.set(group,{id:group,name:filters.overlay === 'genre' ? r.genre || 'Unknown' : r.name,bins:{},...empty()});
        const g=groups.get(group);
        for (const k of metrics) g[k]+=r[k];
        g.bins[key]=(g.bins[key] || 0)+(measure === 'minutes' ? r.seconds/60 : r[historyMetric]);
        if (!details.has(r.track || r.name)) details.set(r.track || r.name,{id:r.track || r.name,name:catalog[r.track]?.title || r.name,artist:r.name,...empty()});
        const d=details.get(r.track || r.name); for (const k of metrics) d[k]+=r[k];
    }
    const series=[], cursor=date(bucket(bounds.start || earliest,period)), end=bucket(shift(bounds.end,-1),period);
    while(stamp(cursor)<=end && series.length<12000) {
        const key=stamp(cursor); series.push(buckets.get(key) || bin(key));
        if(period === 'month') cursor.setUTCMonth(cursor.getUTCMonth()+1);
        else cursor.setUTCDate(cursor.getUTCDate()+(period === 'week' ? 7 : 1));
    }
    if(stamp(cursor)<=end) throw new RangeError('This selection has more than 12,000 periods. Choose a shorter range or a wider grouping.');
    const overlays=[...groups.values()].filter(g=>measure === 'minutes' ? g.seconds : g[historyMetric])
        .sort((a,b)=>(measure === 'minutes' ? b.seconds-a.seconds : b[historyMetric]-a[historyMetric]) || a.name.localeCompare(b.name)).slice(0,30)
        .map(g=>({...g,count:measure === 'minutes' ? g.seconds/60 : g[historyMetric],values:series.map(s=>{
            const value=g.bins[s.day] || 0;
            return measure === 'share' ? (s[historyMetric] ? value/s[historyMetric]*100 : 0) : value;
        })}));
    const time=formats.map(key=>({key,seconds:series.reduce((n,b)=>n+b.secondsByFormat[key],0),plays:series.reduce((n,b)=>n+b.formats[key],0)})).filter(r=>r.seconds || r.plays);
    const discoveryStart=data.discovery_from && (!bounds.start || bounds.start<data.discovery_from) ? data.discovery_from : bounds.start;
    const eligible=data.discovery_from ? selected.filter(r=>r.day>=discoveryStart) : [];
    const returning=eligible.filter(r=>r.plays && catalog[r.track]?.cohort?.first<discoveryStart);
    const established=eligible.reduce((n,r)=>n+r.plays,0)>=60 && new Set(eligible.filter(r=>r.plays).map(r=>r.day)).size>=5
        && returning.reduce((n,r)=>n+r.plays,0)>=20 && new Set(returning.map(r=>r.day)).size>=3;
    const discoveryTotal=empty(), discoveryBins=new Map();
    for(const row of eligible) {
        const key=bucket(row.day,period);
        if(!discoveryBins.has(key)) discoveryBins.set(key,bin(key));
        for(const metric of metrics) {discoveryTotal[metric]+=row[metric]; discoveryBins.get(key)[metric]+=row[metric];}
    }
    const discovery={start:discoveryStart,total:discoveryTotal,series:series.map(s=>discoveryBins.get(s.day) || bin(s.day))};
    const populated=Object.values(total).some(Boolean);
    const cohortSubjects=[...new Set(selected.filter(r=>r.plays).map(r=>r.track))].map(key=>catalog[key]?.cohort).filter(Boolean)
        .filter(c=>data.discovery_from && c.first>=discoveryStart && c.first<bounds.end);
    const cohorts={introduced:cohortSubjects.length,pending:cohortSubjects.filter(c=>c.age<14).length,
        mature:cohortSubjects.filter(c=>c.age>=14).length,
        regulars:cohortSubjects.filter(c=>c.age>=14 && c.days14>=2).length,
        regulars28:cohortSubjects.filter(c=>c.age>=28 && c.days28>=2).length};
    return {total,retained:data.retained || {hearts:0,skulls:0},previous,series,time,overlays,period,historyMetric,measure,cohorts,discovery,
        options:{artists:[...options.artists].map(([id,name])=>({id,name})).sort((a,b)=>a.name.localeCompare(b.name)),genres:[...options.genres].sort()},
        modules:{activity:!!(total.plays || total.sightings || total.seconds),discovery:established,formats:!!time.length,
            feedback:!!(total.hearts || total.skulls || total.clears || total.rated),details:populated},
        details:[...details.values()].filter(r=>metrics.some(k=>r[k])),
        start:bounds.start || earliest,end:bounds.end,compare:!!(filters.compare && selectedSpan),
        comparisonPartial:bounds.end>data.today,timezone:data.timezone || 'UTC',builtAt:data.built_at,today:data.today};
}
let manifest, request=0;
const cache=new Map(); let bytes=0;
async function prepared(filters,id) {
    const bounds=range(manifest,filters);
    let start=bounds.start;
    if(filters.compare && start) start=shift(start,-Math.max(1,(date(bounds.end)-date(start))/86400000));
    const chunks=manifest.chunks.filter(c=>(!start || c.until>=start) && c.from<bounds.end);
    const data={...manifest,rows:[],catalog:{}};
    for(const chunk of chunks) {
        if(id!==request) return null;
        let item=cache.get(chunk.key);
        if(!item) {
            const response=await fetch('/charts/listening-chunk/'+chunk.key);
            if(!response.ok) throw new Error('Generation changed');
            const encoded=await response.text(); item={data:JSON.parse(encoded),bytes:encoded.length*2};
            if(item.bytes>1048576) throw new Error('Oversized listening partition');
            cache.set(chunk.key,item); bytes+=item.bytes;
        } else { cache.delete(chunk.key); cache.set(chunk.key,item); }
        data.rows.push(...item.data.rows); Object.assign(data.catalog,item.data.catalog);
        while(bytes>8*1024*1024 && cache.size>1) {
            const key=cache.keys().next().value; bytes-=cache.get(key).bytes; cache.delete(key);
        }
    }
    return summarize(data,filters);
}
if(typeof self!=='undefined') self.onmessage=async event=>{
    const {id,encoded,filters}=event.data; request=id;
    try {
        if(encoded) manifest=JSON.parse(encoded);
        if(!manifest || manifest.schema!==2) throw new Error('Unsupported listening data');
        const result=await prepared(filters,id);
        if(result && id===request) self.postMessage({id,result});
    } catch(error) { if(id===request) self.postMessage({id,error:error instanceof RangeError ? error.message : 'Could not read listening data. Reload the selection.'}); }
};
if(typeof module!=='undefined') module.exports={summarize,range};
