from functools import lru_cache
from pathlib import Path
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
import time
import math
from collections import defaultdict
from shapely.geometry import shape
from shapely.strtree import STRtree
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
WFS_URL = "https://epub.sjv.se/inspire/opendata/wfs"
WMS_URL = "https://epub.sjv.se/inspire/opendata/wms"
LAYER = "opendata:angs_och_betesmarkinventering"
MAX_FEATURES = 500
DB_PATH = Path(__file__).with_name("field_notes.sqlite3")
DB_LOCK = threading.Lock()
WEATHER_LOCK = threading.Lock()
WEATHER_CACHE = {}
WEATHER_TTL_SECONDS = 1800
SPECIES = {
    "id": "protostropharia_semiglobata",
    "scientific_name": "Protostropharia semiglobata",
    "common_name": "Dung roundhead (gul kragskivling)",
    "habitat": "Herbivore dung and dung-enriched ground, commonly in grazed grassland.",
    "season": "Often summer to autumn in northern Europe; varies with conditions.",
    "identification": "Pale yellow, sticky hemispherical cap; slender stem; darkening gills. Never identify fungi from one feature alone.",
    "data_note": "Registered grazing habitat is assumed eligible unless manually excluded; this is not confirmation of active grazing or dung. Numerical weather optima are not established. Experimental score is not an occurrence probability.",
}

def _inventory_date(feature):
    raw = str((feature.get('properties') or {}).get('inventeringsdatum') or '').strip()
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError):
        return None


def group_inventory_features(features, threshold=0.98):
    """Group near-identical geometry, retaining original record IDs and provenance.

    One representative appears on the map. Different/partially overlapping fields
    remain distinct. Prefer latest documented inventory date, not highest record ID.
    """
    indexed=[]
    for f in features:
        try:
            geom=shape(f['geometry'])
            if not geom.is_valid: geom=geom.buffer(0)
            if geom.is_empty or geom.area<=0: continue
            indexed.append((f,geom))
        except (KeyError,ValueError,TypeError):
            continue
    if not indexed: return []
    geoms=[g for _,g in indexed]
    tree=STRtree(geoms)
    parent=list(range(len(indexed)))
    def root(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    def union(i,j):
        a,b=root(i),root(j)
        if a!=b:parent[b]=a
    for i,gi in enumerate(geoms):
        for j in tree.query(gi):
            j=int(j)
            if j<=i or root(i)==root(j):continue
            gj=geoms[j]
            # inexpensive area-ratio rejection before geometric intersection
            if min(gi.area,gj.area)/max(gi.area,gj.area)<threshold:continue
            try:
                intersection=gi.intersection(gj).area
                iou=intersection/(gi.area+gj.area-intersection)
                if iou>=threshold:union(i,j)
            except Exception:continue
    groups=defaultdict(list)
    for i in range(len(indexed)):groups[root(i)].append(i)
    output=[]
    for indices in groups.values():
        members=[indexed[i][0] for i in indices]
        dated=[(i,_inventory_date(f)) for i,f in enumerate(members)]
        valid_dates=[(i,d) for i,d in dated if d is not None]
        if valid_dates:
            latest=max(d for _,d in valid_dates)
            candidates=[i for i,d in valid_dates if d==latest]
            ambiguous=len(candidates)>1 and len({str((members[i].get('properties') or {}).get('markslag')) for i in candidates})>1
            winner=candidates[0]
        else:
            winner=0;ambiguous=len(members)>1
        chosen=dict(members[winner]);props=dict(chosen.get('properties') or {})
        ids=sorted({str((m.get('properties') or {}).get('id') or m.get('id') or '') for m in members})
        ids=[i for i in ids if i]
        props['_group_ids']=ids
        props['_source_records']=[{
            'id':str((m.get('properties') or {}).get('id') or m.get('id') or ''),
            'markslag':(m.get('properties') or {}).get('markslag'),
            'betesmarktyp':(m.get('properties') or {}).get('betesmarktyp'),
            'inventeringsdatum':(m.get('properties') or {}).get('inventeringsdatum')
        } for m in members]
        props['_group_ambiguous']=ambiguous
        props['_group_count']=len(members)
        props['_has_dated_record']=bool(valid_dates)
        chosen['properties']=props
        output.append(chosen)
    return output


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mushroom Habitat Explorer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; color:#24362d; }
  #map { position:absolute; inset:0 0 232px 280px; }
  #weatherPanel {position:absolute;bottom:0;left:280px;right:0;height:232px;background:#fff;border-top:1px solid #d5dfd4;padding:12px 20px;z-index:600;overflow:auto;}
  .weather-head {display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;}
  .weather-modes {display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;}
  .weather-modes button {font-size:12px;padding:5px 10px;}
  .weather-modes button.active {background:#287c60;color:#fff;border-color:#287c60;}
  #weatherGraph {display:flex;gap:5px;align-items:end;height:104px;margin:9px auto 0;max-width:880px;border-bottom:1px solid #cbd7ca;overflow-x:auto;overflow-y:hidden;}
  #weatherGraph.forecast .weather-col {flex:1 0 0;}
  #weatherGraph.history .weather-col {flex:0 0 34px;}
  #weatherGraph.history {justify-content:flex-start;}
  #weatherGraph.forecast {justify-content:center;}
  .weather-range {display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11px;color:#546458;margin-top:6px;}
  .weather-range input {width:58px;padding:3px 5px;border:1px solid #bdccbe;border-radius:4px;font-size:11px;}
  .weather-range[hidden]{display:none;}
  .weather-col {flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;justify-content:end;height:100%;font-size:10px;}
  .weather-col .bar {min-height:2px;width:65%;background:#5896b9;border-radius:3px 3px 0 0;}
  .weather-col .day {white-space:nowrap;font-size:10px;color:#536254;}
  .weather-col .val {white-space:nowrap;font-weight:600;font-size:10px;}
  #weatherMessage {font-size:12px;color:#536254;margin-top:7px;}
  aside { position:absolute; inset:0 auto 0 0; width:280px; padding:20px;
          background:#f8faf7; overflow:auto; border-right:1px solid #dae1d8; }
  h1 { font-size:19px; margin:0 0 6px; }
  .muted { color:#65736a; font-size:13px; line-height:1.5; }
  .section { margin-top:22px; }
  .option { display:flex; gap:9px; align-items:center; margin:13px 0; font-size:14px; }
  input[type=checkbox] { accent-color:#287c60; width:17px; height:17px; }
  input[type=text], select, textarea { width:100%; padding:8px; border:1px solid #bacabc; border-radius:6px; background:white; font:inherit; }
  textarea { min-height:70px; resize:vertical; }
  .field { margin:11px 0; }
  .field label { display:block; margin-bottom:5px; font-size:13px; font-weight:600; }
  .hint { font-size:12px; color:#65736a; }
  .species { background:#eaf2e7; border-radius:9px; padding:12px; font-size:13px; line-height:1.45; }
  .species p { margin:6px 0; }
  #editPanel { display:none; position:absolute; z-index:1200; top:20px; right:20px; width:335px; max-height:calc(100vh - 40px); overflow:auto; background:#fff; border:1px solid #cbd7ca; border-radius:12px; padding:17px; box-shadow:0 7px 28px #0003; }
  #editPanel h2 { font-size:18px; margin:0 0 5px; }
  .panel-actions {display:flex; gap:8px; align-items:center; margin-top:12px;}
  .save {background:#287c60; color:white; border-color:#287c60;}
  .detail {font-size:13px; line-height:1.5; margin:12px 0; }
  .detail div {margin:4px 0;}
  .swatch { width:13px; height:13px; border-radius:3px; display:inline-block; }
  .map-status-icon {font-size:19px;text-align:center;line-height:27px;background:#fff;border:1px solid #56725b;border-radius:50%;width:29px;height:29px;box-shadow:0 2px 5px #0004;}
  .map-tools {display:flex;flex-wrap:wrap;gap:5px;margin-top:9px;}
  .map-tools button {padding:7px 9px;font-size:12px;}
  .map-tools button.selected {background:#287c60;color:white;}
  #geoPanel {display:none;position:absolute;z-index:1350;top:20px;right:20px;width:345px;max-height:calc(100vh - 40px);overflow:auto;background:white;border:1px solid #cbd7ca;border-radius:12px;padding:17px;box-shadow:0 7px 28px #0003;}
  #geoPanel h2 {margin:0 0 8px;font-size:18px;}
  .geo-view-details {font-size:13px;line-height:1.65;word-break:break-word;}
  .geo-view-details p {margin:7px 0;}
  .obs-dot {width:14px;height:14px;border-radius:50%;background:#14191a;border:2px solid white;box-shadow:0 0 0 1px #252b2e;}
  .heat-legend {display:flex;align-items:center;gap:7px;font-size:11px;color:#536254;margin-top:5px}
  .habitat-halo { border-radius:50%; pointer-events:none; background:radial-gradient(circle,rgba(13,141,52,var(--halo-alpha)) 0%,rgba(31,169,66,calc(var(--halo-alpha)*0.86)) 27%,rgba(52,193,83,calc(var(--halo-alpha)*0.47)) 57%,rgba(90,185,115,0) 100%); }
  .heat-swatch {width:90px;height:9px;border-radius:5px;background:linear-gradient(90deg,#d7e8cf,#75bc83,#248452)}
  #status { margin-top:18px; padding:10px; background:#e9eee8; border-radius:8px;
            font-size:13px; line-height:1.45; }
  button { padding:9px 13px; border:1px solid #a6b8aa; border-radius:7px;
           background:white; cursor:pointer; }
  .leaflet-popup-content { line-height:1.65; }
  @media(max-width:700px){
    #geoPanel {top:195px;right:8px;left:8px;width:auto;max-height:calc(100vh - 205px);}
    #editPanel {top:195px;right:8px;left:8px;width:auto;max-height:calc(100vh - 205px);}
    .species {display:none;}
    aside { width:100%; height:185px; inset:0 0 auto 0; padding:10px 14px; }
    h1 { font-size:16px; }
    .section { margin-top:7px; }
    .option { display:inline-flex; margin:3px 9px 3px 0; font-size:12px; }
    .muted { font-size:11px; }
    #map { inset:185px 0 225px 0; }
    #weatherPanel {left:0;height:225px;padding:8px 10px;}
    .weather-col .day {font-size:9px;}
    .heat-legend {display:flex;align-items:center;gap:7px;font-size:11px;color:#536254;margin-top:5px}
  .heat-swatch {width:90px;height:9px;border-radius:5px;background:linear-gradient(90deg,#d7e8cf,#75bc83,#248452)}
  #status { margin-top:5px; padding:4px; }
  }
</style>
</head>
<body>
<aside>
  <h1>🍄 Mushroom Habitat Explorer</h1>
  <div class="muted">Västra Götaland · Version 0.12A</div>
  <div class="section species">
    <strong>Selected mushroom</strong>
    <p><em>{{ species.scientific_name }}</em><br>{{ species.common_name }}</p>
    <p><b>Habitat:</b> {{ species.habitat }}</p>
    <p><b>Season:</b> {{ species.season }}</p>
    <p><b>Identification:</b> {{ species.identification }}</p>
    <p class="hint">{{ species.data_note }}</p>
  </div>
  <div class="section">
    <strong>Land classifications</strong>
    <div id="filters"></div>
  </div>
  <div class="section">
    <strong>My locations</strong>
    <label class="option"><input id="hideUnsuitable" type="checkbox"> Hide unsuitable polygons</label>
    <label class="option"><input id="onlyAnnotated" type="checkbox"> Show only annotated polygons</label>
  </div>
  <div class="section">
    <label class="option"><input id="wmsToggle" type="checkbox"> Official map overlay (WMS)</label>
    <label class="option"><input id="vectorToggle" type="checkbox" checked> Clickable polygons (WFS)</label>
  </div>
  <div class="section"><strong>Add to map</strong>
    <div class="map-tools"><button id="drawArea">Draw area</button><button id="addPoint">Add observation point</button><button id="cancelMode">Cancel</button></div>
    <p class="hint" id="drawHint">Draw area: click vertices, click first vertex to finish. Point: click once to place. Right-click the map to place a point without selecting a tool.</p>
    <label class="option"><input type="checkbox" id="showCustom" checked> My areas</label>
    <label class="option"><input type="checkbox" id="showPoints" checked> Observation dots</label>
    <label class="option"><input type="checkbox" id="heatToggle" checked> Field-anchored hotspot halos (experimental)</label>
    <label for="haloThreshold">Minimum provisional fruiting-weather index: <b id="haloThresholdLabel">70</b>/100</label>
    <input id="haloThreshold" type="range" min="0" max="100" step="5" value="70" style="width:100%">
    <div class="heat-legend"><span class="heat-swatch"></span> Brighter green = stronger mapped candidate cluster</div>
    <p class="hint" id="heatStatus">Preparing regional overview…</p>
    <button type="button" id="compareWeather">Compare weather across visible map</button>
    <div class="hint" id="weatherComparison" aria-live="polite">On demand: sample four locations within the current view. This does not change halos.</div>
    <p class="hint">Registered grazing land is considered eligible unless marked unsuitable or with no grazing observed; ungrazed meadows are not assumed eligible. Each halo is fixed to one qualifying mapped habitat. Nearby halos overlap naturally. The user-adjustable score threshold is provisional and cannot guarantee growth. No halo is not evidence of absence. Weather estimates are requested near each individual field location and cached. Nearby fields can share model cells; requested coordinates do not guarantee field-scale observations. The graph reports weather only, independent of mapped habitat; halos additionally require eligible pasture. The scoring rules remain unvalidated.</p>
  </div>
  <div id="status">Move or zoom the map to explore.</div>
  <div class="section"><button id="reload">Refresh visible area</button></div>
  <p class="muted">Data: Jordbruksverket. Inventory polygons are not verified current grazing land. No occurrence predictions yet.</p>
</aside>
<div id="map"></div>
<div id="geoPanel" role="dialog" aria-label="Map feature editor">
  <h2 id="geoTitle">Add area</h2>
  <p class="hint" id="geoLocation"></p>
  <div id="geoRead" class="geo-view-details" hidden></div>
  <div id="geoForm">
  <div id="areaInputs">
    <div class="field"><label for="areaName">Area name</label><input id="areaName" type="text" maxlength="120" placeholder="e.g. West meadow"></div>
    <div class="field"><label for="habitatType">Habitat type</label><select id="habitatType"><option value="pasture">Pasture</option><option value="meadow">Meadow</option><option value="forest">Forest</option><option value="grassland">Other grassland</option><option value="other">Other / unknown</option></select></div>
  </div>
  <div id="pointInputs" hidden>
    <div class="field"><label>Species</label><input id="pointSpecies" value="Protostropharia semiglobata" readonly></div>
    <div class="field"><label>Date observed</label><input id="pointDate" type="date"></div>
    <div class="field"><label>Identification status</label><select id="pointConfidence"><option value="tentative">Tentative (user report)</option><option value="confident">Confident (user report)</option></select></div>
  </div>
  <div class="field"><label for="geoNotes">Notes</label><textarea id="geoNotes" maxlength="2000"></textarea></div>
  </div>
  <div class="panel-actions"><button id="editGeo" hidden>Edit</button><button class="save" id="saveGeo">Save</button><button id="deleteGeo" hidden>Delete</button><button id="closeGeo">Close</button></div>
  <div class="hint" id="geoMessage"></div>
</div>
<section id="weatherPanel" aria-label="Weather information">
  <div class="weather-head">
    <strong>Weather: <span id="weatherLocation">Map centre</span></strong>
    <div><span class="hint" id="weatherDates"></span></div>
  </div>
  <div class="weather-modes">
    <button type="button" data-period="forecast" class="active">Next 7 days</button>
    <button type="button" data-period="history">Previous 30 days</button>
    <select id="weatherMetric" aria-label="Weather variable" style="width:auto;max-width:180px;padding:5px">
      <option value="suitability" selected>Species fruiting-weather index (0–100)</option>
      <option value="rain">Rainfall (mm)</option>
      <option value="temperature">Min / max temperature (°C)</option>
      <option value="moisture_surface">Soil moisture 0–7 cm (m³/m³)</option>
      <option value="moisture_root">Soil moisture 7–28 cm (m³/m³)</option>
      <option value="humidity">Relative humidity (%)</option>
      <option value="wind">Maximum wind (km/h)</option>
      <option value="et0">Reference ET₀ (mm)</option>
    </select>
  </div>
  <div class="weather-range" id="weatherRange" hidden>
    <strong>Experimental reference band</strong>
    <label>Min <input id="rangeLow" type="number" step="any"></label>
    <label>Max <input id="rangeHigh" type="number" step="any"></label>
    <span id="rangeUnits"></span>
    <span>Green = within your selected range; amber = near; red = far outside. Not validated species thresholds.</span>
  </div>
  <div id="weatherGraph"></div>
  <div id="weatherMessage">Loading weather…</div>
  <div class="hint" id="suitabilityNote" hidden>Provisional fruiting-weather index for <em>Protostropharia semiglobata</em>. It does not verify dung, grazing or fungal presence and is not a finding probability. Moisture and temperature use exploratory responses; rainfall history, cooling, brief frost and season apply cautious multipliers. These are not measured species optima. See model details below.</div>
  <details id="modelDetails" style="font-size:11px;margin-top:5px"><summary>How the experimental score is calculated</summary><p>Published ecology: dung/manured grassland and a broad warm-season to autumn fruiting period. Model assumptions (unvalidated): current 0–7 cm volumetric soil moisture contributes 50%, preceding seven-day average moisture contributes 20%, and separately assessed daily minimum and maximum temperatures contribute 30%. All response ranges are provisional engineering assumptions, NOT validated biological thresholds. Optional seasonal multiplier is deliberately NOT applied because UK seasonal ranges cannot reliably be transferred to Sweden. If essential weather data are absent, the index is unavailable. The temperature responses are smooth Gaussian curves. Moisture uses a conservative asymmetric response centred on an illustrative 0.27 m³/m³, penalising wet values less strongly because soil texture/field capacity are unknown. Temperature centres are 9°C overnight and 14°C daytime; they have NOT been established empirically for dung roundhead. The three components are for software testing and should not be interpreted as physiological optima, occurrence probabilities, or field-scale predictions.</p></details>
</section>
<div id="editPanel" role="dialog" aria-label="Field notes">
  <div style="display:flex;justify-content:space-between;gap:8px;align-items:start;">
    <h2>Field notes</h2><button type="button" id="closePanel">Close</button>
  </div>
  <div id="fieldSummary" class="detail"></div>
  <div class="field"><label for="assessment">My assessment</label>
    <select id="assessment"><option value="unknown">Unassessed</option><option value="promising">👍 Promising / suitable</option><option value="unsuitable">🚫 Unsuitable / irrelevant</option></select>
  </div>
  <div class="field"><label class="option"><input id="visited" type="checkbox"> I have visited this area</label></div>
  <div class="field"><label for="grazing">Grazing activity</label>
    <select id="grazing"><option value="unknown">Unknown</option><option value="confirmed">Confirmed grazing</option><option value="none_observed">No grazing observed</option></select>
  </div>
  <div class="field"><label for="access">Reported access</label>
    <select id="access"><option value="unknown">Unknown</option><option value="accessible">Appears accessible / permission reported</option><option value="permission_required">Permission required / unclear</option><option value="denied">Access denied</option></select>
  </div>
  <div class="field"><label for="observation">Species observation</label>
    <select id="observation"><option value="not_surveyed">Not surveyed</option><option value="observed">Observed</option><option value="not_observed">Searched, not observed</option></select>
  </div>
  <div class="field"><label for="notes">Notes</label><textarea id="notes" maxlength="2000" placeholder="Your field notes..."></textarea></div>
  <div class="hint">Dated notes are stored on this computer. Access status is user-reported, not an authoritative legal determination. A search with no sighting is not proof of absence.</div>
  <div class="panel-actions"><button id="saveNotes" type="button" class="save">Save notes</button><span id="saveStatus" class="hint" role="status"></span></div>
  <div id="lastSaved" class="hint" style="margin-top:8px;"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const MAP = L.map('map', {preferCanvas:true}).setView([58.1, 12.85], 10);
// Separate high pane: dot observations must remain above both official and custom polygons.
MAP.createPane('regionalHeat');
MAP.getPane('regionalHeat').style.zIndex='345';
MAP.getPane('regionalHeat').style.pointerEvents='none';
MAP.createPane('observationTop');
MAP.getPane('observationTop').style.zIndex='675';
MAP.getPane('observationTop').style.pointerEvents='auto';
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom:19, attribution:'© OpenStreetMap contributors'
}).addTo(MAP);
const WMS = L.tileLayer.wms({{ wms_url|tojson }}, {
  layers: {{ layer|tojson }}, format:'image/png', transparent:true,
  version:'1.3.0', attribution:'Jordbruksverket'
}).addTo(MAP);
const markerLayer = L.layerGroup().addTo(MAP);
let currentFieldScore=null;
function suitabilityColor(score){
  if(score===null || !Number.isFinite(score))return '#9da8a0';
  if(score<25)return '#cf6258';
  if(score<45)return '#e49e58';
  if(score<65)return '#d7cf63';
  if(score<80)return '#95be68';
  return '#388b63';
}
function isEligibleOfficial(props){
  // Grazing land is an eligibility proxy, NOT proof of manure or fruiting.
  const type=String(props?.markslag||'').trim().toLowerCase();
  const pasture=String(props?.betesmarktyp||'').trim();
  if(type.includes('restaurer') || type.includes('ej aktuell') || pasture.toLowerCase().includes('restaurer') || pasture.toLowerCase().includes('ej aktuell'))return false;
  return type==='bete' || Boolean(pasture);
}
function suitEnabled(){return false;} // Original field shading disabled; regional overlay supersedes it.
function mapStyle(feature){
  const original=colorFor(feature.properties.markslag);
  const eligible=isEligibleOfficial(feature.properties);
  const shaded=suitEnabled() && eligible && currentFieldScore!==null;
  return {color:original,fillColor:shaded?suitabilityColor(currentFieldScore):original,
    fillOpacity:shaded?.76:.38,weight:1.7};
}

let annotationById={};
let haloAnnotations={};
let haloNotesReady=false;
// Load the complete saved exclusion table, including custom areas, before drawing halos.
async function refreshHaloNotes(){
  const response=await fetch('/api/halo-annotations/all?species='+encodeURIComponent(speciesId),{cache:'no-store'});
  if(!response.ok)throw Error('Could not refresh hotspot exclusions');
  const obj=await response.json();
  haloAnnotations=obj.notes||{};
  haloNotesReady=true;
  drawHeat();
}

const vectorLayer = L.geoJSON(null, {
  style: mapStyle,
  onEachFeature: (feature, layer) => {
    const p = feature.properties;
    layer.bindPopup(
      `<b>ID:</b> ${esc(p.id)}<br>` +
      `<b>Markslag:</b> ${esc(p.markslag || 'Unknown')}<br>` +
      `<b>Betesmarktyp:</b> ${esc(p.betesmarktyp || 'Unknown')}<br>` +
      `<b>Areal:</b> ${esc(p.areal ?? 'Unknown')} ha<br>` +
      `<b>Inventerad:</b> ${esc(p.inventeringsdatum || 'Unknown')}` +
      (p._group_count>1?`<br><b>Merged inventory records:</b> ${p._group_count}<br>${p._source_records.map(r=>`${esc(r.id)}: ${esc(r.markslag||'?')} (${esc(r.inventeringsdatum||'undated')})`).join('<br>')}`:'') +
      (p._group_ambiguous?'<br><b>Classification ambiguous — review inventory dates</b>':'') +
      `<br><button type="button" class="open-notes">Field notes</button>`
    );
    layer.on('click', (evt) => {
      if(drawMode==='point'){
        L.DomEvent.stopPropagation(evt);
        setDrawMode(null);
        openGeo('point',null,{lat:evt.latlng.lat,lon:evt.latlng.lng});
        return;
      }
      if(drawMode==='area'){
        L.DomEvent.stopPropagation(evt);vertices.push(evt.latlng);renderDraft();return;
      }
      L.DomEvent.stopPropagation(evt);
      const ctr=layer.getBounds().getCenter();
      p._weatherLat=Number.isFinite(p._halo_lat)?p._halo_lat:ctr.lat;
      p._weatherLon=Number.isFinite(p._halo_lon)?p._halo_lon:ctr.lng;
      weatherField={id:String(p.id),lat:p._weatherLat,lon:p._weatherLon};
      requestWeather();
      if(annotationById[String(p.id)]) {
        layer.closePopup();
        openNotes(p);
      }
    });
    layer.on("popupopen", evt => {
      const btn=evt.popup.getElement()?.querySelector(".open-notes");
      if(btn) btn.addEventListener("click", () => {
        const ctr=layer.getBounds().getCenter();
        p._weatherLat=Number.isFinite(p._halo_lat)?p._halo_lat:ctr.lat;
      p._weatherLon=Number.isFinite(p._halo_lon)?p._halo_lon:ctr.lng;
        openNotes(p);
      }, {once:true});
    });
  }
}).addTo(MAP);

// User geography is a separate layer from official WFS data.
const ownAreaLayer=L.featureGroup().addTo(MAP);
const ownPointLayer=L.layerGroup().addTo(MAP);
const drawLayer=L.featureGroup().addTo(MAP);
let ownAreas=[], ownPoints=[], drawMode=null, vertices=[], pendingGeometry=null, editingGeo=null;
let drawPreview=null;
const geoPanel=document.getElementById('geoPanel');
function setDrawMode(mode){
  drawMode=mode; vertices=[]; drawLayer.clearLayers(); pendingGeometry=null;
  MAP.doubleClickZoom.enable();
  if(mode==='area') MAP.doubleClickZoom.disable();
  MAP.getContainer().style.cursor=mode?'crosshair':'';
  document.querySelectorAll('.map-tools button').forEach(b=>b.classList.remove('selected'));
  if(mode==='area')document.getElementById('drawArea').classList.add('selected');
  if(mode==='point')document.getElementById('addPoint').classList.add('selected');
  document.getElementById('drawHint').textContent=mode==='area'?'Click vertices, then click the FIRST vertex (white circle) to finish the polygon.':mode==='point'?'Click anywhere on the map to add an observation.':'Draw area: click vertices and click first vertex to finish. Right-click to add an observation.';
}
document.getElementById('drawArea').onclick=()=>setDrawMode('area');
document.getElementById('addPoint').onclick=()=>setDrawMode('point');
document.getElementById('cancelMode').onclick=()=>setDrawMode(null);
function renderDraft(){
  drawLayer.clearLayers();
  if(vertices.length>1) L.polyline(vertices,{color:'#225c48',weight:3,dashArray:'5 5'}).addTo(drawLayer);
  vertices.forEach((p,i)=>{
    const marker=L.circleMarker(p,{radius:i===0?8:5,color:'#245d47',fillColor:'white',fillOpacity:1,weight:2}).addTo(drawLayer);
    if(i===0 && vertices.length>=3)marker.on('click', ev=>{ L.DomEvent.stopPropagation(ev);finishArea();});
  });
}
function finishArea(){
  if(vertices.length<3)return;
  const coords=vertices.map(p=>[p.lng,p.lat]); coords.push([...coords[0]]);
  const geometry={type:'Polygon',coordinates:[coords]};
  setDrawMode(null);openGeo('area',null,{geometry});
}
MAP.on('click',e=>{
  if(drawMode==='point'){setDrawMode(null);openGeo('point',null,{lat:e.latlng.lat,lon:e.latlng.lng});}
  else if(drawMode==='area'){vertices.push(e.latlng);renderDraft();}
  else if(geoPanel.style.display==='block'){closeGeo();}
});
MAP.on('contextmenu',e=>{if(drawMode==='area')return;setDrawMode(null);openGeo('point',null,{lat:e.latlng.lat,lon:e.latlng.lng});});
function showGeoRead(kind,id,record){
  editingGeo={kind,id,record};
  geoPanel.style.display='block';
  document.getElementById('geoTitle').textContent=kind==='area'?'Area details':'Observation details';
  document.getElementById('geoRead').hidden=false;
  document.getElementById('geoForm').hidden=true;
  document.getElementById('editGeo').hidden=false;
  document.getElementById('saveGeo').hidden=true;
  document.getElementById('deleteGeo').hidden=true;
  document.getElementById('geoLocation').textContent='';
  document.getElementById('geoMessage').textContent='';
  document.getElementById('geoRead').innerHTML=kind==='area'?
    `<p><b>Name:</b> ${esc(record.name)}</p><p><b>Habitat:</b> ${esc(record.habitat)}</p><p><b>Source:</b> User-defined polygon</p><p><b>Notes:</b> ${esc(record.notes||'None')}</p><p><b>Last updated:</b> ${esc(record.updated_at||'Unknown')}</p>`:
    `<p><b>Species:</b> ${esc(record.species_id)}</p><p><b>Date:</b> ${esc(record.observed_on)}</p><p><b>ID confidence:</b> ${esc(record.confidence)}</p><p><b>Coordinates:</b> ${Number(record.lat).toFixed(5)}, ${Number(record.lon).toFixed(5)}</p><p><b>Notes:</b> ${esc(record.notes||'None')}</p>`;
}
function openGeo(kind,id,record){
  editingGeo={kind,id,record}; geoPanel.style.display='block';
  document.getElementById('geoRead').hidden=true;
  document.getElementById('geoForm').hidden=false;
  document.getElementById('editGeo').hidden=true;
  document.getElementById('saveGeo').hidden=false;
  document.getElementById('geoTitle').textContent=(id?'Edit ':'Add ')+(kind==='area'?'area':'observation');
  document.getElementById('areaInputs').hidden=kind!=='area';
  document.getElementById('pointInputs').hidden=kind!=='point';
  document.getElementById('deleteGeo').hidden=!id;
  document.getElementById('geoNotes').value=record.notes||'';
  document.getElementById('geoMessage').textContent='';
  if(kind==='area'){
    document.getElementById('areaName').value=record.name||'';
    document.getElementById('habitatType').value=record.habitat||'other';
    document.getElementById('geoLocation').textContent='User-drawn boundary · independent of official GIS areas';
  }else{
    document.getElementById('pointDate').value=record.observed_on||new Date().toLocaleDateString('sv-SE');
    document.getElementById('pointConfidence').value=record.confidence||'tentative';
    document.getElementById('geoLocation').textContent=`Coordinates: ${Number(record.lat).toFixed(5)}, ${Number(record.lon).toFixed(5)}`;
  }
}
document.getElementById('editGeo').onclick=()=>{
  if(editingGeo){const {kind,id,record}=editingGeo;openGeo(kind,id,record);}
};
function closeGeo(){geoPanel.style.display='none';editingGeo=null;pendingGeometry=null;}
document.getElementById('closeGeo').onclick=closeGeo;
async function saveOrDeleteGeo(del=false){
  if(!editingGeo)return;
  const {kind,id,record}=editingGeo;
  const payload=kind==='area'?{
    name:document.getElementById('areaName').value.trim(), habitat:document.getElementById('habitatType').value,
    notes:document.getElementById('geoNotes').value, geometry:record.geometry
  }: {lat:record.lat, lon:record.lon, species_id:speciesId,
    observed_on:document.getElementById('pointDate').value, confidence:document.getElementById('pointConfidence').value,
    notes:document.getElementById('geoNotes').value};
  if(!del && kind==='area'&&!payload.name){document.getElementById('geoMessage').textContent='Enter a name.';return;}
  const endpoint='/api/my-'+(kind==='area'?'areas':'observations')+(id?'/'+id:'');
  try{
    const response=await fetch(endpoint,{method:del?'DELETE':id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:del?undefined:JSON.stringify(payload)});
    const obj=await response.json(); if(!response.ok)throw Error(obj.error||'Request failed');
    closeGeo();await loadMyGeography();
  }catch(ex){document.getElementById('geoMessage').textContent=ex.message;}
}
document.getElementById('saveGeo').onclick=()=>saveOrDeleteGeo(false);
document.getElementById('deleteGeo').onclick=()=>{if(confirm('Delete this saved map feature?'))saveOrDeleteGeo(true);};
async function loadMyGeography(){
  try{
    const [ar,pt]=await Promise.all([fetch('/api/my-areas'),fetch('/api/my-observations')]);
    if(!ar.ok||!pt.ok)throw Error('Unable to load saved map features');
    ownAreas=(await ar.json()).items;ownPoints=(await pt.json()).items;
    const customIds=ownAreas.map(a=>'custom:'+a.id);
    if(customIds.length){
      const notes=await Promise.all(customIds.map(id=>fetch('/api/notes/'+encodeURIComponent(id)+'?species='+encodeURIComponent(speciesId)).then(r=>r.json()).then(d=>[id,d.note]).catch(()=>[id,null])));
      for(const [id,n] of notes){if(n)annotationById[id]=n;}
    }
    drawOwnGeography();
    drawHeat();
  }catch(err){document.getElementById('geoMessage').textContent='Load error: '+err.message;}
}
function drawOwnGeography(){
  ownAreaLayer.clearLayers();ownPointLayer.clearLayers();
  if(document.getElementById('showCustom').checked){
    for(const a of ownAreas){
      const eligible=a.habitat==='pasture';
      const shaded=suitEnabled() && eligible && currentFieldScore!==null;
      const layer=L.geoJSON(a.geometry,{style:{color:'#168354',weight:3,
        fillColor:shaded?suitabilityColor(currentFieldScore):'#42a879',fillOpacity:shaded?.7:.15}}).addTo(ownAreaLayer);
      layer.bindTooltip(esc(a.name)+' (my area)');
      const fieldId='custom:'+a.id;
      const fieldProps={id:fieldId,markslag:a.habitat,betesmarktyp:a.habitat==='pasture'?'User pasture':'',areal:'User-defined',_weatherLat:null,_weatherLon:null};
      const centre=layer.getBounds().getCenter();
      fieldProps._weatherLat=centre.lat;fieldProps._weatherLon=centre.lng;
      layer.bindPopup(`<b>${esc(a.name)}</b><br><b>Habitat:</b> ${esc(a.habitat)}<br>`+
        `<b>Area source:</b> User-created<br>`+
        `<button type="button" class="custom-notes">Field notes</button> `+
        `<button type="button" class="custom-geometry">Edit</button>`);
      layer.on('popupopen',ev=>{
        const element=ev.popup.getElement();if(!element)return;
        element.querySelector('.custom-notes')?.addEventListener('click',()=>{layer.closePopup();openNotes(fieldProps);});
        element.querySelector('.custom-geometry')?.addEventListener('click',()=>{layer.closePopup();openGeo('area',a.id,a);});
      });
      layer.on('click',ev=>{
        if(drawMode==='point'){L.DomEvent.stopPropagation(ev);setDrawMode(null);openGeo('point',null,{lat:ev.latlng.lat,lon:ev.latlng.lng});layer.closePopup();}
        else if(drawMode==='area'){L.DomEvent.stopPropagation(ev);vertices.push(ev.latlng);renderDraft();layer.closePopup();}
        else {L.DomEvent.stopPropagation(ev);weatherField={id:a.name,lat:centre.lat,lon:centre.lng};requestWeather();
          if(annotationById[fieldId]){layer.closePopup();openNotes(fieldProps);}}
      });
    }
  }
  if(document.getElementById('showPoints').checked){
    for(const p of ownPoints){
      const icon=L.divIcon({className:'',html:'<div class="obs-dot"></div>',iconSize:[14,14],iconAnchor:[7,7]});
      L.marker([p.lat,p.lon],{icon,pane:'observationTop',zIndexOffset:1000}).addTo(ownPointLayer)
        .bindTooltip(esc(p.species_id)+' · '+esc(p.observed_on))
        .on('click',ev=>{L.DomEvent.stopPropagation(ev);showGeoRead('point',p.id,p);});
    }
  }
}
document.getElementById('showCustom').onchange=()=>{drawOwnGeography();drawHeat();};
document.getElementById('showPoints').onchange=drawOwnGeography;

// Independent sampling diagnostic: no map re-creation or halo changes.
const comparisonEl=document.getElementById('weatherComparison');
document.getElementById('compareWeather').addEventListener('click',async()=>{
  const b=MAP.getBounds(), w=b.getWest(), e=b.getEast(), so=b.getSouth(), n=b.getNorth();
  comparisonEl.textContent='Comparing local weather at four sampling points…';
  try {
    const r=await fetch(`/api/weather-grid?bbox=${[w,so,e,n].map(v=>v.toFixed(5)).join(',')}`);
    const obj=await r.json();
    if(!r.ok)throw Error(obj.error||'Weather comparison failed');
    const table=document.createElement('table');
    table.style.cssText='font-size:11px;width:100%;margin-top:6px;border-collapse:collapse';
    const head=document.createElement('tr');
    ['Sample','Temp min/max °C','Rain mm','0–7cm m³/m³'].forEach(v=>{const h=document.createElement('th');h.textContent=v;h.style.textAlign='left';head.appendChild(h)});
    table.appendChild(head);
    for(const pt of obj.samples){
      const row=document.createElement('tr');
      const cells=[pt.label,pt.temperature_min===null?'—':`${pt.temperature_min} / ${pt.temperature_max}`,pt.rainfall??'—',pt.moisture_surface??'—'];
      cells.forEach((v,j)=>{const c=document.createElement('td');c.textContent=String(v);c.style.padding='4px 2px';row.appendChild(c)});
      row.style.cursor='pointer';row.title='Click to inspect weather at this point';
      row.addEventListener('click',()=>{weatherField={id:pt.label,lat:pt.requested_lat,lon:pt.requested_lon};requestWeather()});
      table.appendChild(row);
    }
    comparisonEl.replaceChildren(table);
    const note=document.createElement('p');note.textContent='Gridded model weather, not on-site measurements. Click a sample to show its full weather graph. Forecast values for today, not a fruiting probability.';comparisonEl.appendChild(note);
  }catch(err){comparisonEl.textContent='Comparison unavailable: '+err.message}
});
const speciesId={{ species.id|tojson }};
loadMyGeography();
let activeField=null;
let weatherField=null;
const editPanel=document.getElementById('editPanel');
const ids=['assessment','grazing','access','observation','notes'];
const defaults={assessment:'unknown',grazing:'unknown', access:'unknown',observation:'not_surveyed',notes:'',visited:false};
async function openNotes(p) {
  activeField=String(p.id);
  weatherField={id:String(p.id), lat:p._weatherLat, lon:p._weatherLon};
  if(Number.isFinite(weatherField.lat) && Number.isFinite(weatherField.lon)) requestWeather();
  editPanel.style.display='block';
  document.getElementById('fieldSummary').innerHTML =
    `<div><b>ID:</b> ${esc(p.id)}</div><div><b>Land type:</b> ${esc(p.markslag || 'Unknown')}</div>`+
    `<div><b>Pasture type:</b> ${esc(p.betesmarktyp || 'Unknown')}</div>`+
    `<div><b>Area:</b> ${esc(p.areal ?? 'Unknown')} ha</div>`;
  ids.forEach(k=>{document.getElementById(k).value=defaults[k]});
  document.getElementById('visited').checked=false;
  document.getElementById('saveStatus').textContent='Loading…';
  document.getElementById('lastSaved').textContent='';
  const idAtRequest=activeField;
  const groupIds=p._group_ids||[idAtRequest];
  window.activeGroupIds=groupIds;
  try {
    const r=await fetch('/api/notes/group?species='+encodeURIComponent(speciesId)+'&ids='+encodeURIComponent(groupIds.join(',')));
    const data=await r.json();
    if(!r.ok) throw Error(data.error || 'Load failed');
    if(activeField!==idAtRequest) return;
    if(data.note) {
      ids.forEach(k=>{document.getElementById(k).value=data.note[k] ?? defaults[k]});
      document.getElementById('visited').checked=!!data.note.visited;
      document.getElementById('lastSaved').textContent='Last saved: '+data.note.updated_at;
    }
    document.getElementById('saveStatus').textContent='';
  } catch(e) { if(activeField===idAtRequest) document.getElementById('saveStatus').textContent='Load error: '+e.message; }
}
document.getElementById('closePanel').addEventListener('click',()=>{ editPanel.style.display='none';activeField=null; MAP.closePopup(); weatherField=null;requestWeather();});
document.getElementById('saveNotes').addEventListener('click',async()=>{
  if(!activeField) return;
  const idAtRequest=activeField;
  const payload={species_id:speciesId};
  ids.forEach(k=>payload[k]=document.getElementById(k).value);
  payload.visited=document.getElementById('visited').checked;
  document.getElementById('saveStatus').textContent='Saving…';
  try {
    const r=await fetch('/api/notes/group', {
      method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({...payload,field_ids:window.activeGroupIds||[idAtRequest]})
    });
    const data=await r.json();
    if(!r.ok) throw Error(data.error || 'Save failed');
    if(activeField!==idAtRequest) return;
    document.getElementById('saveStatus').textContent='Saved';
    document.getElementById('lastSaved').textContent='Last saved: '+data.note.updated_at;
    for(const gid of (window.activeGroupIds||[idAtRequest])){annotationById[gid]=data.note;haloAnnotations[gid]={assessment:data.note.assessment,grazing:data.note.grazing};}
    updateVisible();
    drawHeat();
    refreshHaloNotes().catch(e=>{heatStatus.textContent='Exclusion refresh error: '+e.message;});
  } catch(e) {if(activeField===idAtRequest) document.getElementById('saveStatus').textContent='Save error: '+e.message;}
});
const categories = [
  ['Bete','#31976B'],
  ['Äng','#E5C44F'],
  ['Restaurerbar','#DF9145'],
  ['Ej aktuell','#8C959F'],
  ['Other / unknown','#7D77AC']
];
const filtersEl = document.getElementById('filters');
for (const [name,color] of categories) {
  const label = document.createElement('label');
  label.className='option';
  const input = document.createElement('input');
  input.type='checkbox'; input.checked = true; input.dataset.category=name;
  const swatch=document.createElement('span'); swatch.className='swatch';
  swatch.style.background=color;
  label.append(input,swatch,document.createTextNode(name));
  filtersEl.append(label);
  input.addEventListener('change', updateVisible);
}
function category(value) {
  return categories.some(c=>c[0]===value) ? value : 'Other / unknown';
}
function colorFor(value) {
  return (categories.find(c=>c[0]===category(value)) || categories[4])[1];
}
function esc(x) {
  return String(x).replace(/[&<>"']/g, c=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
let features=[], latestRequest=0, controller=null, timer=null, lastTruncated=false;
const status=document.getElementById('status');
const vectorToggle=document.getElementById('vectorToggle');
const wmsToggle=document.getElementById('wmsToggle');
function statusIcon(note) {
  if(!note) return '';
  if(note.observation==='observed') return '🍄';
  if(note.assessment==='promising') return '👍';
  if(note.assessment==='unsuitable') return '🚫';
  if(note.visited) return '✓';
  return '✎';
}
function updateVisible() {
  vectorLayer.clearLayers();
  markerLayer.clearLayers();
  if(!vectorToggle.checked || MAP.getZoom()<11) return;
  const selected = new Set([...filtersEl.querySelectorAll('input:checked')].map(x=>x.dataset.category));
  const hide=document.getElementById('hideUnsuitable').checked;
  const only=document.getElementById('onlyAnnotated').checked;
  const visible=features.filter(f=>{
    const note=(f.properties?._group_ids||[String(f.properties?.id)]).map(id=>annotationById[id]).find(Boolean);
    return selected.has(category(f.properties?.markslag)) &&
      (!hide || note?.assessment!=='unsuitable') && (!only || !!note);
  });
  vectorLayer.addData({type:'FeatureCollection',features:visible});
  vectorLayer.eachLayer(layer => {
    const note=(layer.feature.properties?._group_ids||[String(layer.feature.properties?.id)]).map(id=>annotationById[id]).find(Boolean);
    if(!note) return;
    const icon=statusIcon(note);
    const center=layer.getBounds().getCenter();
    L.marker(center, {interactive:false, icon:L.divIcon({
      className:'', html:`<div class="map-status-icon">${icon}</div>`,
      iconSize:[29,29], iconAnchor:[14,14]
    })}).addTo(markerLayer);
  });
  status.textContent=`${visible.length} visible / ${features.length} retrieved polygons`+
    (lastTruncated?' · Result limit reached: area may be incomplete':'');
}

async function loadViewport() {
  const id=++latestRequest;
  if(controller) controller.abort();
  controller=new AbortController();
  if(!vectorToggle.checked || MAP.getZoom()<11) {
    features=[]; lastTruncated=false; vectorLayer.clearLayers();
    status.textContent='Zoom to level 11+ to load clickable polygons. WMS remains available.';
    return;
  }
  const b=MAP.getBounds();
  const bbox=[b.getWest(),b.getSouth(),b.getEast(),b.getNorth()].map(v=>v.toFixed(5)).join(',');
  status.textContent='Loading polygons for visible area…';
  try {
    const res=await fetch(`/api/polygons?bbox=${encodeURIComponent(bbox)}`,{signal:controller.signal});
    const data=await res.json();
    if(!res.ok) throw Error(data.error || 'Request failed');
    if(id!==latestRequest) return;
    features=data.features || [];
    lastTruncated=!!data.truncated;
    annotationById={};
    const ids=[...new Set(features.flatMap(f=>f.properties?._group_ids||[String(f.properties?.id||'')]).filter(Boolean))];
    if(ids.length) {
      const ares=await fetch('/api/notes/batch', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({species_id:speciesId,field_ids:ids}), signal:controller.signal
      });
      if(!ares.ok) throw Error('Failed loading annotation markers');
      const answer=await ares.json();
      if(id!==latestRequest) return;
      annotationById=answer.notes || {};
    }
    updateVisible();
  } catch(e) {
    if(e.name!=='AbortError' && id===latestRequest) status.textContent='GIS error: '+e.message;
  }
}
function queueLoad() { clearTimeout(timer); timer=setTimeout(loadViewport,500); }
MAP.on('moveend',()=>{ queueLoad(); if(typeof drawHeat==='function')drawHeat(); queueHeat(); if(typeof queueLocalScores==='function')queueLocalScores(); if(!weatherField) queueWeather(); });
vectorToggle.addEventListener('change',loadViewport);
['hideUnsuitable','onlyAnnotated'].forEach(id=>document.getElementById(id).addEventListener('change',updateVisible));
wmsToggle.addEventListener('change',()=>{
  if(wmsToggle.checked) WMS.addTo(MAP); else MAP.removeLayer(WMS);
});
document.getElementById('reload').addEventListener('click',loadViewport);
loadViewport();

// REGIONAL OVERVIEW: geographically aggregated WFS habitat density, not
// biological occurrence prediction. Raster-like soft circles are non-interactive.
const regionalHeat=L.layerGroup().addTo(MAP);
let heatTimer=null, heatRequest=0, heatController=null, cachedHeatCells=[];
const heatToggle=document.getElementById('heatToggle');
const heatStatus=document.getElementById('heatStatus');
// Soft green screen-space markers: low zoom = broad area cue,
// high zoom = smaller, more localised cue. No rectangle or red backdrop.
// One stable geographic anchor per feature. No clustering or averaging:
// a field marker must never jump to a neighbour when zoom changes.
const rememberedHabitatAnchors = new Map();
let localWeatherSamples=[];
let localWeatherReady=false;
let localWeatherRequest=0, localWeatherTimer=null, localWeatherAbort=null;
// A field is always evaluated at its own stable coordinate, rounded to ~1 km
// only for request caching. Never snap to viewport-dependent sample positions.
function weatherKey(lat,lng){return `${(Math.round(lat*100)/100).toFixed(2)},${(Math.round(lng*100)/100).toFixed(2)}`;}
const fieldScoreCache=new Map();
const fieldScorePending=new Set();
function localScoreFor(lat,lng){
  const p=fieldScoreCache.get(weatherKey(lat,lng));
  return p && Number.isFinite(p.score)?p.score:null;
}
function collectWeatherPoints(){
  const bounds=MAP.getBounds().pad(.12), center=MAP.getCenter();
  const points=new Map();
  for(const p of rememberedHabitatAnchors.values()){
    if(bounds.contains([p.lat,p.lng])){
      const key=weatherKey(p.lat,p.lng);
      if(!fieldScoreCache.has(key) && !fieldScorePending.has(key))points.set(key,{lat:p.lat,lon:p.lng});
    }
  }
  if(document.getElementById('showCustom').checked){
    for(const a of ownAreas){
      if(a.habitat!=='pasture')continue;
      const ring=a.geometry?.coordinates?.[0];
      if(!ring?.length)continue;
      const vertices=ring.slice(0,-1);
      let x=0,y,twice=0; y=0;
      for(let i=0;i<vertices.length;i++){
        const p=vertices[i],q=vertices[(i+1)%vertices.length];
        const cross=p[0]*q[1]-q[0]*p[1];
        twice+=cross;x+=(p[0]+q[0])*cross;y+=(p[1]+q[1])*cross;
      }
      const lon=Math.abs(twice)>1e-12?x/(3*twice):vertices[0][0];
      const lat=Math.abs(twice)>1e-12?y/(3*twice):vertices[0][1];
      const key=weatherKey(lat,lon);
      if(bounds.contains([lat,lon])&&!fieldScoreCache.has(key)&&!fieldScorePending.has(key))points.set(key,{lat,lon});
    }
  }
  return [...points.values()].sort((a,b)=>
    Math.hypot(a.lat-center.lat,a.lon-center.lng)-Math.hypot(b.lat-center.lat,b.lon-center.lng));
}
async function loadLocalScores(){
  if(!heatToggle.checked||MAP.getZoom()<8)return;
  const seq=++localWeatherRequest;
  if(localWeatherAbort)localWeatherAbort.abort();
  localWeatherAbort=new AbortController();
  const points=collectWeatherPoints().slice(0,75);
  if(!points.length){localWeatherReady=true;drawHeat();return;}
  points.forEach(p=>fieldScorePending.add(weatherKey(p.lat,p.lon)));
  heatStatus.textContent=`Loading field-specific weather (${points.length} locations)…`;
  try{
    const r=await fetch('/api/field-scores',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({points}),signal:localWeatherAbort.signal});
    const data=await r.json();
    if(!r.ok)throw Error(data.error||'Field weather request failed');
    for(const row of (data.samples||[]))fieldScoreCache.set(weatherKey(row.lat,row.lon),row);
    localWeatherReady=true;
    localWeatherSamples=[...fieldScoreCache.values()];
    if(seq===localWeatherRequest)drawHeat();
  }catch(e){if(e.name!=='AbortError')heatStatus.textContent='Field weather unavailable: '+e.message;}
  finally{
    points.forEach(p=>fieldScorePending.delete(weatherKey(p.lat,p.lon)));
    if(seq===localWeatherRequest && collectWeatherPoints().length)queueLocalScores();
  }
}
function queueLocalScores(){clearTimeout(localWeatherTimer);localWeatherTimer=setTimeout(loadLocalScores,650);}

function haloRadiusPx(zoom, isUser=false){
  // Prominent at broad zoom; gradually smaller in SCREEN pixels when zoomed in.
  // A fixed lat/lon gives the expected converging circles when zooming out.
  const byZoom={8:62,9:58,10:51,11:44,12:35,13:28,14:23,15:19};
  const radius=byZoom[Math.max(8,Math.min(15,Math.round(zoom)))] || 17;
  return Math.round(radius*(isUser?.85:1));
}
function addHalo(latlng, strength, user=false){
  const radius=haloRadiusPx(MAP.getZoom(),user);
  // Stronger centre and broad fading edge, layered transparently over base map.
  const alpha=Math.min(.90,Math.max(.61,.54+.32*((strength-70)/30)));
  const diameter=2*radius;
  const icon=L.divIcon({className:'',
    html:`<div class="habitat-halo" style="--halo-alpha:${alpha.toFixed(3)};width:${diameter}px;height:${diameter}px"></div>`,
    iconSize:[diameter,diameter],iconAnchor:[radius,radius]});
  L.marker(latlng,{pane:'regionalHeat',icon,interactive:false,keyboard:false}).addTo(regionalHeat);
}
function rememberHabitats(cells){
  // Store feature anchors across viewport requests. Old positions do not get
  // recomputed from the centre of a query cell or from grouped markers.
  for(const cell of cells){
    if(cell.incomplete)continue;
    for(const p of (cell.points||[])){
      if(!Number.isFinite(p.lat)||!Number.isFinite(p.lng))continue;
      const id=p.id != null ? 'official:'+String((p.group_ids||[]).slice().sort()[0]||p.id) :
        'coordinate:'+p.lat.toFixed(6)+':'+p.lng.toFixed(6);
      const old=rememberedHabitatAnchors.get(id);
      const ids=[...new Set([...(old?.groupIds||[]),...(p.group_ids||[String(p.id)])])];
      rememberedHabitatAnchors.set(id,{lat:p.lat,lng:p.lng,weight:p.area_ha||0,groupIds:ids});
    }
  }
}
function drawHeat(){
  regionalHeat.clearLayers();
  if(!heatToggle.checked || MAP.getZoom()<8)return;
  if(!haloNotesReady){heatStatus.textContent='Loading saved field exclusions…';return;}
  if(!localWeatherReady){heatStatus.textContent='Loading field-specific weather…';return;}
  // Provisional environmental threshold, NOT a species-specific guarantee.
  const minimumScore=Number(document.getElementById('haloThreshold').value);
  const b=MAP.getBounds().pad(.25);
  let count=0, duplicates=0;
  // Exclusions are applied BEFORE spatial deduplication. Use a small spatial
  // threshold to collapse duplicated/overlapping GIS records with essentially
  // identical anchors. Nearby distinct fields keep their own separate halos.
  const occupied=new Set();
  function spatialKey(lat,lng){
    // Around 8–12 m at this latitude, for duplicate WFS geometry anchors.
    return `${Math.round(lat*10000)}:${Math.round(lng*10000)}`;
  }
  function addOnce(lat,lng,user=false){
    const score=localScoreFor(lat,lng);
    if(score===null || score<minimumScore)return false;
    const key=spatialKey(lat,lng);
    if(occupied.has(key)){duplicates++;return false;}
    occupied.add(key);addHalo([lat,lng],score,user);count++;
    return true;
  }
  for(const [key,p] of rememberedHabitatAnchors.entries()){
    if(!b.contains([p.lat,p.lng]))continue;
    const id=key.replace('official:','');
    const groupIds=p.groupIds||[id];
    if(groupIds.some(gid=>{const n=haloAnnotations[gid]||annotationById[gid];return n?.assessment==='unsuitable'||n?.grazing==='none_observed';}))continue;
    addOnce(p.lat,p.lng);
    if(count>=700)break;
  }
  if(document.getElementById('showCustom').checked){
    for(const area of ownAreas){
      if(area.habitat!=='pasture')continue;
      const n=haloAnnotations['custom:'+area.id] || annotationById['custom:'+area.id];
      if(n?.assessment==='unsuitable' || n?.grazing==='none_observed')continue;
      // A polygon interior point is computed from original field geometry.
      let ring=area.geometry?.coordinates?.[0];
      if(!ring||ring.length<4)continue;
      const vertices=ring.slice(0,-1);
      // Polygon centroid; fall back to actual vertex for strongly concave areas.
      let x=0,y=0,twice=0;
      for(let i=0;i<vertices.length;i++){
        const p=vertices[i],q=vertices[(i+1)%vertices.length];
        const cross=p[0]*q[1]-q[0]*p[1];
        twice+=cross;x+=(p[0]+q[0])*cross;y+=(p[1]+q[1])*cross;
      }
      const latlng=Math.abs(twice)>1e-12?[y/(3*twice),x/(3*twice)]:[vertices[0][1],vertices[0][0]];
      if(!L.polygon(ring.map(c=>[c[1],c[0]])).getBounds().contains(latlng)){
        latlng[0]=vertices[0][1];latlng[1]=vertices[0][0];
      }
      if(b.contains(latlng))addOnce(latlng[0],latlng[1],true);
    }
  }
  heatStatus.textContent=`${count} habitat halos (${duplicates} duplicate positions suppressed). Nearby different fields can still overlap. Field-coordinate species fruiting-weather index (provisional); ${localWeatherSamples.length} cached locations.`;
}
async function loadHeat(){
  const requestId=++heatRequest;
  if(heatController) heatController.abort();
  heatController=new AbortController();
  if(!heatToggle.checked || MAP.getZoom()<8){
    regionalHeat.clearLayers();
    heatStatus.textContent=MAP.getZoom()<8?'Zoom to level 8+ for the grazing-habitat overview.':'Overview hidden.';
    return;
  }
  const b=MAP.getBounds();
  const bbox=[b.getWest(),b.getSouth(),b.getEast(),b.getNorth()].map(v=>v.toFixed(4)).join(',');
  heatStatus.textContent='Loading candidate habitat clusters…';
  try{
    const r=await fetch(`/api/habitat-grid?bbox=${encodeURIComponent(bbox)}`,{signal:heatController.signal});
    const data=await r.json();
    if(!r.ok)throw Error(data.error || 'Grid unavailable');
    if(requestId!==heatRequest)return;
    cachedHeatCells=data.cells||[];
    rememberHabitats(cachedHeatCells);
    // Always refresh ALL exclusions, not only fields returned by this viewport.
    // This also resolves custom-area exclusions after reloading the browser.
    await refreshHaloNotes();
    if(requestId!==heatRequest)return;
    drawHeat();
    queueLocalScores();
    if(data.incomplete)heatStatus.textContent+=` ${data.incomplete} grid cells had incomplete GIS responses and are omitted.`;
  }catch(e){if(e.name!=='AbortError' && requestId===heatRequest){
    heatStatus.textContent='Regional layer unavailable: '+e.message;
  }}
}
function queueHeat(){clearTimeout(heatTimer);heatTimer=setTimeout(loadHeat,800);}
document.getElementById('haloThreshold').addEventListener('input',e=>{document.getElementById('haloThresholdLabel').textContent=e.target.value;drawHeat();if(weatherData)drawWeather();});
heatToggle.addEventListener('change',()=>{if(!heatToggle.checked){regionalHeat.clearLayers();heatStatus.textContent='Overview hidden.';}else {queueHeat();queueLocalScores();}});
refreshHaloNotes().catch(e=>{heatStatus.textContent='Could not read saved exclusions: '+e.message;});
queueHeat();
queueLocalScores();

// Weather visualisation uses daily values from the Open-Meteo forecast API.
// The previous days are gridded/model-based estimates, not station observations.
let weatherData=null;
let weatherPeriod='forecast';
let weatherRequest=0;
let weatherTimer=null;
const weatherMetric=document.getElementById('weatherMetric');
const weatherGraph=document.getElementById('weatherGraph');
const weatherMessage=document.getElementById('weatherMessage');
function queueWeather(){clearTimeout(weatherTimer);weatherTimer=setTimeout(requestWeather,650);}
document.querySelectorAll('[data-period]').forEach(btn=>btn.addEventListener('click',()=>{
  weatherPeriod=btn.dataset.period;
  document.querySelectorAll('[data-period]').forEach(b=>b.classList.toggle('active',b===btn));
  drawWeather();
}));
weatherMetric.addEventListener('change',drawWeather);
const rangeWrap=document.getElementById('weatherRange');
const rangeLow=document.getElementById('rangeLow');
const rangeHigh=document.getElementById('rangeHigh');
const rangeUnits=document.getElementById('rangeUnits');
const ranges={temperature:[5,15],moisture_surface:[0.20,0.35],moisture_root:[0.20,0.35]};
// Experimental and UNCALIBRATED index: fixed absolute ranges, never month-relative.
// Test assumptions, NOT established physiological thresholds for dung roundhead.
function clamp01(x){return Math.min(1,Math.max(0,x));}
function trapezoid(x, zeroLow, fullLow, fullHigh, zeroHigh){
  if(!Number.isFinite(x))return null;
  if(x<=zeroLow||x>=zeroHigh)return 0;
  if(x>=fullLow&&x<=fullHigh)return 1;
  return x<fullLow?(x-zeroLow)/(fullLow-zeroLow):(zeroHigh-x)/(zeroHigh-fullHigh);
}
function gaussianResponse(value, center, sigma){
  if(!Number.isFinite(value))return null;
  return Math.exp(-0.5*Math.pow((value-center)/sigma,2));
}
// Conservative, asymmetric moisture response: high volumetric moisture is not
// automatically waterlogging without field capacity/soil texture information.
// Exploratory heuristic only, NOT a species-specific biological optimum.
function moistureResponse(value){
  return gaussianResponse(value,.27,value<.27?.085:.18);
}
function experimentalSuitability(days,index,today){
  const row=days[index];
  if(![row.moisture_surface,row.tempMin,row.tempMax].every(Number.isFinite))return null;
  if(row.tempMin>row.tempMax)return null;
  const window=days.slice(Math.max(0,index-6),index+1).map(d=>d.moisture_surface).filter(Number.isFinite);
  if(window.length<4)return null;
  const avg=window.reduce((a,b)=>a+b,0)/window.length;
  const surface=moistureResponse(row.moisture_surface);
  const sustained=moistureResponse(avg);
  const night=gaussianResponse(row.tempMin,9,8);
  const day=gaussianResponse(row.tempMax,14,9);
  return Math.round(1000*(.50*surface+.20*sustained+.30*(night+day)/2))/10;
}


let previousRangeMetric=null;
[rangeLow,rangeHigh].forEach(input=>input.addEventListener('input',()=>{
  const metric=weatherMetric.value;
  const low=Number(rangeLow.value),high=Number(rangeHigh.value);
  if(rangeLow.value!=='' && rangeHigh.value!=='' && Number.isFinite(low) && Number.isFinite(high) && low<high){
    ranges[metric]=[low,high];drawWeather();
  }
}));
function referenceColor(v,low,high){
  if(typeof v!=='number'||!Number.isFinite(v))return '#b6bdc0';
  if(v>=low&&v<=high)return '#3f9a67';
  const width=Math.max(0.001,high-low);
  return (v<low?low-v:v-high)<=width*0.35?'#d9a347':'#d45c52';
}
// A Leaflet popup can close as a SIDE EFFECT of selecting a new field or
// reloading GIS layers. Never interpret popupclose as deselection.
// Selection persists until the user explicitly clicks the map background,
// closes the notes panel, or chooses another field.
MAP.on('click', () => {
  if(editPanel.style.display === 'block' || !weatherField)return;
  weatherField=null;
  requestWeather();
});
async function requestWeather(){
  const n=++weatherRequest;
  const ctr=MAP.getCenter();
  // Field forecast MUST use its anchored, zoom-independent grid cell, matching
  // halo eligibility. The map-centre forecast instead uses the actual map
  // coordinates: snapping the centre to a coarse lattice caused hard jumps
  // as users panned a few metres across a weather-grid boundary.
  const sample=weatherField
    ? {lat:weatherField.lat,lon:weatherField.lon}
    : {lat:ctr.lat,lon:ctr.lng};
  const lat=sample.lat;
  const lon=sample.lon;
  document.getElementById('weatherLocation').textContent=weatherField
    ? `Field ${weatherField.id} · local model requested near ${lat.toFixed(4)}°N, ${lon.toFixed(4)}°E`
    : `Map centre · local model weather ${lat.toFixed(3)}°N, ${lon.toFixed(3)}°E`;
  // Environmental index only; land classification is evaluated separately.
  weatherMessage.textContent='Loading daily weather…';
  try {
    const r=await fetch(`/api/weather?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`);
    const data=await r.json();
    if(!r.ok) throw Error(data.error || 'Unable to retrieve weather');
    if(n!==weatherRequest) return;
    weatherData=data;drawWeather();
  } catch(e){if(n===weatherRequest){weatherGraph.replaceChildren();weatherMessage.textContent='Weather unavailable: '+e.message;}}
}
function drawWeather(){
  if(!weatherData) return;
  const metric=weatherMetric.value;
  const d=weatherData.daily;
  const all=d.time.map((date,i)=>({
    date, rain:d.precipitation_sum?.[i],
    temperature:d.temperature_2m_mean?.[i],
    tempMin:d.temperature_2m_min?.[i],tempMax:d.temperature_2m_max?.[i],
    humidity:d.relative_humidity_2m_mean?.[i],
    wind:d.wind_speed_10m_max?.[i], et0:d.et0_fao_evapotranspiration?.[i],
    moisture_surface:d.soil_moisture_0_to_7cm?.[i],
    moisture_root:d.soil_moisture_7_to_28cm?.[i]
  }));
  const today=weatherData.today;
  all.forEach(row=>row.suitability=weatherData.species_daily_scores?.[row.date] ?? null);
  // Source of truth for TODAY: identical server calculation used by halos.
  // JS keeps calculating the other six forecast days from the same weather data.
  const todayIndex=all.findIndex(row=>row.date===today);
  if(todayIndex>=0)all[todayIndex].suitability=Number.isFinite(weatherData.halo_index)?weatherData.halo_index:null;
  // Weather suitability is only a regional context estimate at the chosen
  // weather coordinate. Mask shading strictly to eligible mapped habitat.
  const todaysRow=all.find(row=>row.date===today);
  const nextScore=todaysRow && Number.isFinite(todaysRow.suitability)?todaysRow.suitability:null;
  if(!weatherField)currentFieldScore=nextScore; // Graph-only context; halos use local grid scores.

  const selected=all.filter(x=>weatherPeriod==='forecast'?x.date>=today:x.date<today);
  const rows=weatherPeriod==='forecast'?selected.slice(0,7):selected.slice(-30);
  const tempMetric=metric==='temperature';
  const suitMetric=metric==='suitability';
  document.getElementById('suitabilityNote').hidden=!suitMetric;
  document.getElementById('modelDetails').hidden=!suitMetric;
  const values=rows.flatMap(x=>tempMetric?[x.tempMin,x.tempMax]:[x[metric]])
    .filter(v=>typeof v==='number' && Number.isFinite(v));
  const max=suitMetric?100:Math.max(1,...values);
  const min=suitMetric?0:Math.min(0,...values);
  const range=Math.max(0.001,max-min);
  weatherGraph.replaceChildren();
  weatherGraph.className=weatherPeriod;
  const isRangeMetric=Object.hasOwn(ranges,metric);
  rangeWrap.hidden=!isRangeMetric;
  if(isRangeMetric){
    if(previousRangeMetric!==metric){
      rangeLow.value=ranges[metric][0];rangeHigh.value=ranges[metric][1];
      previousRangeMetric=metric;
    }
    rangeUnits.textContent=metric==='temperature'?'°C':'m³/m³';
  } else previousRangeMetric=null;
  const digits=metric.startsWith('moisture_')?3:1;
  for(const row of rows){
    const col=document.createElement('div');col.className='weather-col';
    const v=row[metric];
    const low=row.tempMin, high=row.tempMax;
    const value=document.createElement('span');value.className='val';
    const validTemp=typeof low==='number'&&typeof high==='number';
    value.textContent=tempMetric?(validTemp?`${low.toFixed(0)}–${high.toFixed(0)}`:'–'):
      (typeof v==='number'?v.toFixed(digits):'–');
    const bar=document.createElement('div');bar.className='bar';
    if(suitMetric){
      bar.style.height=(typeof v==='number'?Math.max(2,65*v/100):2)+'px';
      bar.style.background=typeof v!=='number'?'#b6bdc0':v>=75?'#378d56':v>=50?'#92b35b':v>=25?'#d9a347':'#d45c52';
    } else if(tempMetric){
      // A range bar, floating between the daily minimum and maximum.
      bar.style.height=(validTemp?Math.max(3,65*(high-low)/range):2)+'px';
      bar.style.marginBottom=(validTemp?Math.max(0,65*(low-min)/range):0)+'px';
      bar.style.background=referenceColor((low+high)/2,...ranges.temperature);
    } else {
      bar.style.height=(typeof v==='number'?Math.max(2,65*(v-min)/range):2)+'px';
      if(metric==='humidity')bar.style.background='#4f9d98';
      if(metric==='et0')bar.style.background='#ad8ab8';
      if(metric==='wind')bar.style.background='#91a2a9';
      if(metric.startsWith('moisture_'))bar.style.background=referenceColor(v,...ranges[metric]);
    }
    const day=document.createElement('span');day.className='day';
    day.textContent=weatherPeriod==='forecast'?row.date.slice(5):row.date.slice(8);
    col.title=row.date+': '+value.textContent;
    col.append(value,bar,day);weatherGraph.appendChild(col);
  }
  document.getElementById('weatherDates').textContent=rows.length?`${rows[0].date} to ${rows[rows.length-1].date}`:'';
  const missing=values.length===0;
  const sourceInfo=metric.startsWith('moisture_')
    ?'Gridded model soil moisture, volumetric m³/m³; not a field measurement.'
    :suitMetric?'Provisional species fruiting-weather index; not a mushroom probability or verified field prediction.'
    :'Gridded model weather; not measured at the field.';
  const debug = weatherData.halo_diagnostics;
  const componentInfo=weatherField&&debug?.inputs&&debug?.components
    ? ` Components: current soil moisture ${debug.inputs.soil_moisture_0_to_7cm} m³/m³ → ${debug.components.current_moisture}/100 (50% weight); 7d mean ${debug.inputs.soil_moisture_7d_mean} → ${debug.components.sustained_moisture}/100 (20%); night ${debug.inputs.temperature_min}°C → ${debug.components.night_temperature}/100; day ${debug.inputs.temperature_max}°C → ${debug.components.day_temperature}/100 (temperatures together 30%). Baseline ${debug.baseline_score}/100; rainfall last 3/14 days ${debug.inputs.rainfall_3d_mm ?? 'N/A'}/${debug.inputs.rainfall_14d_mm ?? 'N/A'} mm; previous-week cooling ${debug.inputs.week_cooling_c ?? 'N/A'}°C; frost nights/freezing days (4d) ${debug.inputs.frost_nights_last_4d}/${debug.inputs.freezing_days_last_4d}; provisional rain/cooling/frost/season multipliers ${debug.modifiers.recent_rainfall}/${debug.modifiers.cooling}/${debug.modifiers.frost}/${debug.modifiers.season}.`
    : '';
  const qCounts=weatherData.soil_moisture_daily_sample_counts?.soil_moisture_0_to_7cm;
  const qIndex=weatherData.daily?.time?.indexOf(weatherData.today) ?? -1;
  const qCount=Array.isArray(qCounts) && qIndex>=0 ? qCounts[qIndex] : null;
  const qNote=` Soil-moisture source: Open-Meteo model output, not calculated from rainfall. Today’s shallow-moisture mean uses ${qCount ?? "unknown"} hourly samples. Reported model elevation: ${weatherData.model_elevation_m ?? "unavailable"} m; model grid selection: land/elevation-aware. Differences in model grid cells and soil assumptions may cause sharp spatial changes. High volumetric moisture alone does not establish waterlogging; soil texture and field capacity are unknown.`;
  const haloExplanation=weatherField
    ? ` Field-coordinate weather index for this field today: ${Number.isFinite(weatherData.halo_index)?weatherData.halo_index+'/100':'unavailable'}; threshold ${document.getElementById('haloThreshold').value}/100. A halo additionally requires eligible pasture habitat. Green shading can still come from nearby overlapping halos.${componentInfo}`
    : ` Map-centre WEATHER-ONLY index today: ${Number.isFinite(weatherData.halo_index)?weatherData.halo_index+'/100':'unavailable'}. No land-cover penalty is applied; absence of mapped pastures does not lower this index. It describes weather estimated near the map centre, not an average across the whole view.`;
  weatherMessage.textContent=`Provider model location ${weatherData.latitude.toFixed(4)}°N, ${weatherData.longitude.toFixed(4)}°E; requested ${weatherData.requested_latitude.toFixed(4)}°N, ${weatherData.requested_longitude.toFixed(4)}°E · ${weatherPeriod==='history'?'Past model estimates':'Forecast'} · ${rows.length} days. ${missing?'This variable is unavailable from the selected weather model. ':''}${sourceInfo}${qNote}${haloExplanation} No mushroom probability is calculated.`;
}

requestWeather();
</script>
</body>
</html>"""


@lru_cache(maxsize=96)
def fetch_polygons(bbox):
    params = {
        "service": "WFS", "version": "1.0.0", "request": "GetFeature",
        "typeName": LAYER, "outputFormat": "application/json",
        "srsName": "EPSG:4326", "bbox": ",".join(map(str, bbox)) + ",EPSG:4326",
        "maxFeatures": MAX_FEATURES,
    }
    response = requests.get(WFS_URL, params=params, timeout=35)
    response.raise_for_status()
    data = response.json()
    if data.get("type") != "FeatureCollection":
        raise ValueError("Unexpected WFS response")
    features = data.get("features", [])
    return {"features": features, "truncated": len(features) >= MAX_FEATURES}


@app.route("/")
def index():
    return render_template_string(HTML, wms_url=WMS_URL, layer=LAYER, species=SPECIES)


@app.route("/api/polygons")
def polygons():
    try:
        raw = request.args.get("bbox", "")
        bbox = tuple(round(float(v), 4) for v in raw.split(","))
        if len(bbox) != 4:
            raise ValueError("Expected four coordinates")
        west, south, east, north = bbox
        if not (10 <= west < east <= 25 and 55 <= south < north <= 70):
            raise ValueError("Invalid bounding box")
        if east - west > 1 or north - south > 1:
            raise ValueError("Zoom in further before requesting polygons")
        raw_data=fetch_polygons(bbox)
        grouped=group_inventory_features(raw_data['features'])
        for feature in grouped:
            anchor=field_anchor(feature.get('geometry'))
            if anchor:
                feature.setdefault('properties',{})['_halo_lat']=anchor['lat']
                feature['properties']['_halo_lon']=anchor['lng']
        return jsonify({'features':grouped,'truncated':raw_data['truncated'],
                        'source_count':len(raw_data['features']),
                        'group_count':len(grouped)})
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"error": str(exc)}), 400




def _inside_ring(x, y, ring):
    """Ray-casting test to avoid placing centroids outside concave fields."""
    inside = False
    j = len(ring)-1
    for i in range(len(ring)):
        xi, yi = ring[i][:2]
        xj, yj = ring[j][:2]
        if ((yi > y) != (yj > y)) and x < (xj-xi)*(y-yi)/(yj-yi)+xi:
            inside = not inside
        j = i
    return inside


def _ring_area_centroid(ring):
    twice=0.0; cx=0.0; cy=0.0
    for a,b in zip(ring,ring[1:]):
        cross=a[0]*b[1]-b[0]*a[1]
        twice+=cross;cx+=(a[0]+b[0])*cross;cy+=(a[1]+b[1])*cross
    if abs(twice)<1e-12:
        return 0,ring[0][0],ring[0][1]
    return abs(twice),cx/(3*twice),cy/(3*twice)


def field_anchor(geometry):
    """Return [lat, lon] anchored inside/on actual polygon (not WFS query cell)."""
    if not geometry:return None
    t=geometry.get('type'); coords=geometry.get('coordinates') or []
    polys=[coords] if t=='Polygon' else coords if t=='MultiPolygon' else []
    choices=[]
    for poly in polys:
        if not poly or len(poly[0])<3:continue
        ring=poly[0]
        area,x,y=_ring_area_centroid(ring)
        if not _inside_ring(x,y,ring) or any(_inside_ring(x,y,hole) for hole in poly[1:]):
            # A boundary vertex is still tied to this specific field.
            x,y=ring[0][:2]
        choices.append((area,y,x))
    if not choices:return None
    _,lat,lon=max(choices)
    return {'lat':lat,'lng':lon}


@lru_cache(maxsize=1024)
def habitat_cell(west, south, east, north):
    """Return real eligible habitat anchors, never rectangular grid-centre points."""
    bbox=tuple(round(x,4) for x in (west,south,east,north))
    try:
        data=fetch_polygons(bbox)
        points=[]
        for f in group_inventory_features(data['features']):
            p=f.get('properties') or {}
            mark=str(p.get('markslag') or '').strip().casefold()
            pasture=str(p.get('betesmarktyp') or '').strip()
            if ('restaurer' in mark or 'ej aktuell' in mark or 'restaurer' in pasture.casefold() or 'ej aktuell' in pasture.casefold() or not (mark=='bete' or bool(pasture))):
                continue
            anchor=field_anchor(f.get('geometry'))
            if anchor and west<=anchor['lng']<=east and south<=anchor['lat']<=north:
                anchor['area_ha']=max(0,float(p.get('areal') or 0))
                anchor['id']=str(p.get('id') or f.get('id') or '')
                anchor['group_ids']=p.get('_group_ids',[])
                anchor['source_records']=p.get('_source_records',[])
                points.append(anchor)
        points.sort(key=lambda x:x['area_ha'],reverse=True)
        # Limit serialization; this is an exploratory sampled overview.
        return {'west':west,'south':south,'east':east,'north':north,
                'points':points[:65], 'count':len(points),
                'incomplete':bool(data['truncated'])}
    except (ValueError,TypeError,IndexError,requests.RequestException) as exc:
        return {'west':west,'south':south,'east':east,'north':north,
                'points':[], 'count':0,'incomplete':True,'error':str(exc)}


@app.route('/api/habitat-grid')
def habitat_grid():
    try:
        bbox = tuple(float(v) for v in request.args.get('bbox','').split(','))
        if len(bbox)!=4 or not all(math.isfinite(v) for v in bbox):
            raise ValueError('Invalid bounding box')
        w,s,e,n=bbox
        if not (10<=w<e<=25 and 55<=s<n<=70):
            raise ValueError('Outside supported map extent')
        if e-w>5 or n-s>3:
            raise ValueError('Zoom in for the regional overview')
        # Fixed sixteen subregions at each viewport; computed concurrently.
        nx=4; ny=4
        boxes=[]
        for iy in range(ny):
            for ix in range(nx):
                aw=w+(e-w)*ix/nx; ae=w+(e-w)*(ix+1)/nx
                bs=s+(n-s)*iy/ny; bn=s+(n-s)*(iy+1)/ny
                boxes.append(tuple(round(v,4) for v in (aw,bs,ae,bn)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            cells=list(pool.map(lambda x:habitat_cell(*x),boxes))
        return jsonify({'cells':cells,'incomplete':sum(bool(c['incomplete']) for c in cells)})
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400


def get_weather_at(lat, lon):
    try:
        if not (math.isfinite(lat) and math.isfinite(lon) and 55 <= lat <= 70 and 10 <= lon <= 25):
            raise ValueError("Invalid coordinates")
        # Rounded cell centres limit duplicate API calls while panning.
        key = (round(lat, 2), round(lon, 2))
        with WEATHER_LOCK:
            hit = WEATHER_CACHE.get(key)
            if hit and time.monotonic() - hit[0] < WEATHER_TTL_SECONDS:
                return hit[1]
        daily = ",".join([
            "precipitation_sum", "temperature_2m_mean", "temperature_2m_min",
            "temperature_2m_max", "relative_humidity_2m_mean",
            "wind_speed_10m_max", "et0_fao_evapotranspiration"
        ])
        # Hourly moisture is averaged to a daily value, retaining physical units.
        # Models may not provide both depth ranges. If an API rejects them, fetch
        # weather without moisture, and explicitly show moisture as unavailable.
        moisture_vars = ["soil_moisture_0_to_7cm", "soil_moisture_7_to_28cm"]
        params = {
            "latitude": key[0], "longitude": key[1],
            "timezone": "Europe/Stockholm", "past_days": 30,
            "forecast_days": 7, "daily": daily, "hourly": ",".join(moisture_vars),
            "cell_selection": "land"
        }
        resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=30)
        if not resp.ok:
            params.pop("hourly")
            resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if "daily" not in result or "time" not in result["daily"]:
            raise ValueError("Weather provider returned no daily values")
        moisture_quality = {}
        if "hourly" in result:
            hourly = result["hourly"]
            for var in moisture_vars:
                sums = {}
                for timestamp, value in zip(hourly.get("time", []), hourly.get(var, [])):
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        day = timestamp[:10]
                        total, count = sums.get(day, (0.0, 0))
                        sums[day] = (total + value, count + 1)
                result["daily"][var] = [
                    round(sums[day][0] / sums[day][1], 4) if day in sums else None
                    for day in result["daily"]["time"]
                ]
                moisture_quality[var] = [sums.get(day, (0, 0))[1] for day in result["daily"]["time"]]
        else:
            for var in moisture_vars:
                result["daily"][var] = [None] * len(result["daily"]["time"])
                moisture_quality[var] = [0] * len(result["daily"]["time"])
        result = {
            "requested_latitude": key[0], "requested_longitude": key[1],
            "latitude": result.get("latitude", key[0]),
            "longitude": result.get("longitude", key[1]),
            "model_elevation_m": result.get("elevation"),
            "soil_moisture_source": "Open-Meteo hourly modelled volumetric moisture; arithmetic daily mean (not calculated from rainfall)",
            "soil_moisture_daily_sample_counts": moisture_quality,
            "today": datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Stockholm')).date().isoformat(),
            "daily": result["daily"]
        }
        with WEATHER_LOCK:
            WEATHER_CACHE[key] = (time.monotonic(), result)
            if len(WEATHER_CACHE) > 300:
                WEATHER_CACHE.clear()
        return result
    except (ValueError, requests.RequestException):
        raise


@app.route("/api/weather")
def weather():
    try:
        lat=float(request.args.get("lat","nan"))
        lon=float(request.args.get("lon","nan"))
        data=get_weather_at(lat,lon)
        return jsonify({**data, 'halo_index': local_weather_index(data), 'halo_diagnostics': weather_index_details(data), 'species_daily_scores': forecast_species_scores(data)})
    except (ValueError,requests.RequestException) as exc:
        return jsonify({"error":str(exc)}),502


def today_weather_sample(label,lat,lon):
    data=get_weather_at(lat,lon)
    today=data['today']
    dates=data['daily']['time']
    if today not in dates:
        raise ValueError('Current date unavailable in weather output')
    i=dates.index(today)
    daily=data['daily']
    def val(k):
        series=daily.get(k) or []
        v=series[i] if i<len(series) else None
        return round(float(v),3) if isinstance(v,(int,float)) and math.isfinite(v) else None
    return {
        'label':label,'requested_lat':round(lat,4),'requested_lon':round(lon,4),
        'model_lat':data['latitude'],'model_lon':data['longitude'],
        'date':today,'temperature_min':val('temperature_2m_min'),
        'temperature_max':val('temperature_2m_max'),
        'rainfall':val('precipitation_sum'),
        'moisture_surface':val('soil_moisture_0_to_7cm'),
        'moisture_root':val('soil_moisture_7_to_28cm'),
    }

def _trapezoid(value, zero_low, full_low, full_high, zero_high):
    """Bounded exploratory suitability function; parameters are unvalidated."""
    if value is None or not math.isfinite(value):
        return None
    if value <= zero_low or value >= zero_high:
        return 0.0
    if full_low <= value <= full_high:
        return 1.0
    if value < full_low:
        return (value-zero_low)/(full_low-zero_low)
    return (zero_high-value)/(zero_high-full_high)


def gaussian_response(value, centre, sigma):
    """Exploratory non-flat curve (NOT a validated fungal optimum)."""
    if value is None or not math.isfinite(value):
        return None
    return math.exp(-0.5 * ((value - centre) / sigma) ** 2)


def moisture_response(value):
    """Conservative provisional response: wet soils are not automatically waterlogged.

    Without soil texture or field capacity, very high volumetric water content
    is uncertain rather than proof of unsuitable conditions. A broad wet-side
    response reduces unsupported penalties; this is NOT species calibration.
    """
    return gaussian_response(value, .27, .085 if value < .27 else .18)


def weather_index_details(data, target_date=None):
    """Exploratory P. semiglobata fruiting-weather index (not a probability).

    Calculates from actual provider daily data; same routine supplies field halos,
    detailed field diagnostics and the seven-day weather-index graph. All biological
    response scales and weights are explicitly provisional.
    """
    daily = data['daily']
    dates = daily.get('time', [])
    target_date = target_date or data['today']
    try:
        i = dates.index(target_date)
    except ValueError:
        return {'score': None, 'reason': 'Requested date not in weather data'}

    def number(key, idx):
        values = daily.get(key) or []
        v = values[idx] if 0 <= idx < len(values) else None
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None

    moisture = number('soil_moisture_0_to_7cm', i)
    tmin = number('temperature_2m_min', i)
    tmax = number('temperature_2m_max', i)
    if moisture is None or tmin is None or tmax is None or tmin > tmax:
        return {'score': None, 'reason': 'Missing/invalid moisture or temperature data'}
    counts = data.get('soil_moisture_daily_sample_counts', {}).get('soil_moisture_0_to_7cm', [])
    today_samples = counts[i] if i < len(counts) else None
    moisture_history = [number('soil_moisture_0_to_7cm', j) for j in range(max(0, i-6), i+1)]
    moisture_history = [v for v in moisture_history if v is not None]
    if len(moisture_history) < 4:
        return {'score': None, 'reason': 'Insufficient 7-day moisture history'}
    avg7 = sum(moisture_history)/len(moisture_history)
    current = moisture_response(moisture)
    sustained = moisture_response(avg7)
    night = gaussian_response(tmin, 9, 8)
    day = gaussian_response(tmax, 14, 9)
    thermal = (night+day)/2
    baseline = .50*current + .20*sustained + .30*thermal

    # Rainfall antecedent windows: use as a contextual *small* modifier, not
    # an invented species-specific rainfall trigger. Wet soil already enters
    # the baseline, so rainfall must not be weighted as a second main input.
    def rain_window(days_back):
        values=[number('precipitation_sum',j) for j in range(max(0,i-days_back+1),i+1)]
        if len(values)<days_back or any(v is None for v in values):
            return None
        return round(sum(max(0,v) for v in values),1)
    rain3=rain_window(3)
    rain14=rain_window(14)
    # No positive/negative rainfall adjustment without adequate observations.
    rain_factor=1.0
    if rain14 is not None:
        if rain14 < 2 and avg7 < .15:
            rain_factor=.94
        elif rain14 >= 8 and moisture < .17:
            rain_factor=1.03

    # Temperature-change context over the preceding week, bounded to avoid
    # the rule creating large score jumps. Not an established fruiting trigger.
    earlier_max=[number('temperature_2m_max',j) for j in range(i-7,i-2)] if i>=7 else []
    earlier_max=[v for v in earlier_max if v is not None]
    cooling=None
    cooling_factor=1.0
    if len(earlier_max)>=4:
        cooling=round(sum(earlier_max)/len(earlier_max)-tmax,1)
        if 2 <= cooling <= 9:
            cooling_factor=1.03

    # A frost event is not a permanent end-of-season switch. Modelled air
    # temperature cannot diagnose frozen ground. Stronger suppression applies
    # only when repeated frost coincides with low daytime maxima.
    last4=[(number('temperature_2m_min',j),number('temperature_2m_max',j))
           for j in range(max(0,i-3),i+1)]
    frosts=sum(1 for low,high in last4 if low is not None and low < 0)
    freezing_days=sum(1 for low,high in last4 if high is not None and high <= 0)
    frost_factor=1.0
    if freezing_days>=2:
        frost_factor=.60
    elif freezing_days==1:
        frost_factor=.82
    elif frosts>=3:
        frost_factor=.88
    elif frosts>=1:
        frost_factor=.97

    # Broad season cue for a summer-autumn dung-associated mushroom in Sweden.
    # Do not disqualify exceptional off-season fruiting or enforce a hard cutoff.
    month=int(target_date[5:7])
    season_factor={1:.77,2:.77,3:.82,4:.87,5:.94,6:1.,7:1.,8:1.,9:1.,10:1.,11:.96,12:.84}[month]
    raw=100*baseline
    score=round(min(100,max(0,raw*rain_factor*cooling_factor*frost_factor*season_factor)),1)
    return {
        'score':score,
        'inputs': {'soil_moisture_0_to_7cm':round(moisture,4),
                   'soil_moisture_7d_mean':round(avg7,4),
                   'temperature_min':round(tmin,2),'temperature_max':round(tmax,2),
                   'surface_moisture_hourly_samples_today':today_samples,
                   'rainfall_3d_mm':rain3,'rainfall_14d_mm':rain14,
                   'week_cooling_c':cooling,'frost_nights_last_4d':frosts,
                   'freezing_days_last_4d':freezing_days},
        'components': {'current_moisture':round(current*100,1),
                       'sustained_moisture':round(sustained*100,1),
                       'night_temperature':round(night*100,1),
                       'day_temperature':round(day*100,1)},
        'baseline_score':round(raw,1),
        'modifiers': {'recent_rainfall':rain_factor,'cooling':cooling_factor,
                      'frost':frost_factor,'season':season_factor},
        'weights': {'current_moisture':.50,'sustained_moisture':.20,
                    'temperatures_combined':.30},
        'species':'Protostropharia semiglobata',
        'model':'v0.15 provisional dung-roundhead fruiting-weather profile; none of the numerical optima or modifiers are species-validated'
    }


def local_weather_index(data):
    return weather_index_details(data)['score']


def forecast_species_scores(data):
    dates=data.get('daily',{}).get('time',[])
    return {day:weather_index_details(data,day)['score'] for day in dates}


@app.route('/api/field-scores', methods=['POST'])
def field_scores():
    """Weather-only scores at stable field coordinates, cached at ~1 km resolution.

    Returned model coordinates are those reported by Open-Meteo; they may be
    coarser than the requested spacing. Missing data never become positive scores.
    """
    try:
        payload=request.get_json(silent=True) or {}
        points=payload.get('points')
        if not isinstance(points,list) or len(points)>75:
            raise ValueError('Expected at most 75 field coordinates')
        unique={}
        for p in points:
            lat=float(p['lat']); lon=float(p['lon'])
            if not all(math.isfinite(x) for x in (lat,lon)) or not (55<=lat<=70 and 10<=lon<=25):
                raise ValueError('Invalid field coordinate')
            unique[(round(lat,2),round(lon,2))]=(lat,lon)
        def evaluate(item):
            key,coords=item
            try:
                data=get_weather_at(*coords)
                return {'lat':coords[0],'lon':coords[1],'score':local_weather_index(data),
                        'model_lat':data['latitude'],'model_lon':data['longitude'],
                        'requested_lat':data['requested_latitude'],'requested_lon':data['requested_longitude']}
            except (ValueError,requests.RequestException) as exc:
                return {'lat':coords[0],'lon':coords[1],'score':None,'error':str(exc)}
        with ThreadPoolExecutor(max_workers=5) as pool:
            samples=list(pool.map(evaluate,unique.items()))
        return jsonify(samples=samples,model='weather-only experimental index',
            note='Requests are placed near field coordinates; actual spatial resolution depends on weather provider model.')
    except (ValueError,KeyError,TypeError) as exc:
        return jsonify(error=str(exc)),400


@app.route('/api/local-scores')
def local_scores():
    try:
        bbox=[float(x) for x in request.args.get('bbox','').split(',')]
        if len(bbox)!=4 or not all(map(math.isfinite,bbox)):
            raise ValueError('Invalid viewport')
        w,s,e,n=bbox
        if not (10<=w<e<=25 and 55<=s<n<=70 and e-w<=5 and n-s<=3):
            raise ValueError('Zoom in to score local weather (maximum 5° × 3° viewport)')
        # Fixed geographic lattice: changing the viewport or zoom does not
        # change the coordinates used to evaluate a given location.
        # A one-cell margin ensures nearest-neighbour lookup at viewport edges.
        LAT_STEP = 0.25
        LON_STEP = 0.40
        iy0 = math.floor(s / LAT_STEP) - 1
        iy1 = math.ceil(n / LAT_STEP) + 1
        ix0 = math.floor(w / LON_STEP) - 1
        ix1 = math.ceil(e / LON_STEP) + 1
        total = (iy1-iy0+1)*(ix1-ix0+1)
        if total > 120:
            raise ValueError('Visible area too large for detailed scoring; zoom in one level')
        samples = [
            {'lat': round(j * LAT_STEP, 4), 'lon': round(i * LON_STEP, 4)}
            for j in range(iy0, iy1 + 1)
            for i in range(ix0, ix1 + 1)
            if 55 <= j * LAT_STEP <= 70 and 10 <= i * LON_STEP <= 25
        ]
        def evaluate(p):
            try:
                data=get_weather_at(p['lat'],p['lon'])
                return {**p,'score':local_weather_index(data)}
            except (ValueError,requests.RequestException) as exc:
                return {**p,'score':None,'error':str(exc)}
        with ThreadPoolExecutor(max_workers=3) as pool:
            samples=list(pool.map(evaluate,samples))
        return jsonify(samples=samples,model='experimental weather-only index',
                       note='Experimental non-probabilistic scores. Weather grid coordinates are fixed regardless of viewport zoom.')
    except ValueError as exc:
        return jsonify(error=str(exc)),400


@app.route('/api/weather-grid')
def weather_grid():
    try:
        bbox=[float(x) for x in request.args.get('bbox','').split(',')]
        if len(bbox)!=4 or not all(map(math.isfinite,bbox)):
            raise ValueError('Invalid viewport')
        w,so,e,n=bbox
        if not (10<=w<e<=25 and 55<=so<n<=70 and e-w<=5 and n-so<=3):
            raise ValueError('Zoom in to compare local weather (maximum 5° × 3° viewport)')
        coords=[('NW',so+.75*(n-so),w+.25*(e-w)),
                ('NE',so+.75*(n-so),w+.75*(e-w)),
                ('SW',so+.25*(n-so),w+.25*(e-w)),
                ('SE',so+.25*(n-so),w+.75*(e-w))]
        with ThreadPoolExecutor(max_workers=2) as pool:
            samples=list(pool.map(lambda x:today_weather_sample(*x),coords))
        return jsonify({'samples':samples,'source':'Open-Meteo forecast model',
                        'note':'Four geographically distinct weather samples; no halo modifications.'})
    except (ValueError,requests.RequestException) as exc:
        return jsonify({'error':str(exc)}),502



# User-created features are stored independently of the official WFS polygons.
def validate_area(data):
    if not isinstance(data, dict): raise ValueError("Invalid data")
    name=data.get("name")
    if not isinstance(name,str) or not (1<=len(name.strip())<=120): raise ValueError("Invalid area name")
    if data.get("habitat") not in {"pasture","meadow","forest","grassland","other"}: raise ValueError("Invalid habitat")
    notes=data.get("notes", "")
    if not isinstance(notes,str) or len(notes)>2000: raise ValueError("Invalid notes")
    geometry=data.get("geometry")
    if not isinstance(geometry,dict) or geometry.get("type")!="Polygon": raise ValueError("Expected polygon")
    rings=geometry.get("coordinates")
    if not isinstance(rings,list) or len(rings)!=1 or not isinstance(rings[0],list) or not (4<=len(rings[0])<=201): raise ValueError("Invalid polygon vertices")
    for coord in rings[0]:
        if not isinstance(coord,list) or len(coord)!=2 or any(not isinstance(x,(int,float)) or isinstance(x,bool) or not math.isfinite(x) for x in coord) or not (10<=coord[0]<=25 and 55<=coord[1]<=70):
            raise ValueError("Invalid polygon coordinates")
    if rings[0][0]!=rings[0][-1] or len({tuple(c) for c in rings[0][:-1]})<3: raise ValueError("Polygon must be closed")
    return name.strip(),data["habitat"],notes,geometry

def validate_observation(data):
    if not isinstance(data,dict) or data.get("species_id")!=SPECIES["id"]: raise ValueError("Invalid species")
    lat,lon=data.get('lat'),data.get('lon')
    if any(not isinstance(n,(int,float)) or isinstance(n,bool) or not math.isfinite(n) for n in (lat,lon)) or not (55<=lat<=70 and 10<=lon<=25): raise ValueError("Invalid coordinates")
    date=data.get('observed_on')
    try: datetime.strptime(date,'%Y-%m-%d')
    except (ValueError,TypeError): raise ValueError("Invalid date")
    if data.get('confidence') not in {'tentative','confident'}: raise ValueError("Invalid confidence")
    notes=data.get('notes','')
    if not isinstance(notes,str) or len(notes)>2000: raise ValueError("Invalid notes")
    return lat,lon,date,data['confidence'],notes

@app.route('/api/my-areas',methods=['GET','POST'])
@app.route('/api/my-areas/<int:item_id>',methods=['PUT','DELETE'])
def my_areas(item_id=None):
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory=sqlite3.Row
        if request.method=='GET':
            rows=conn.execute('SELECT * FROM user_areas ORDER BY id').fetchall()
            return jsonify(items=[{**dict(r),'geometry':__import__('json').loads(r['geometry'])} for r in rows])
        if request.method=='DELETE':
            cur=conn.execute('DELETE FROM user_areas WHERE id=?',(item_id,))
            return (jsonify(ok=True) if cur.rowcount else (jsonify(error='Not found'),404))
        try: name,habitat,notes,geometry=validate_area(request.get_json(silent=True))
        except ValueError as e: return jsonify(error=str(e)),400
        encoded=__import__('json').dumps(geometry)
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        if item_id is None:
            conn.execute('INSERT INTO user_areas(name,habitat,notes,geometry,updated_at) VALUES(?,?,?,?,?)',(name,habitat,notes,encoded,now))
        else:
            cur=conn.execute('UPDATE user_areas SET name=?,habitat=?,notes=?,geometry=?,updated_at=? WHERE id=?',(name,habitat,notes,encoded,now,item_id))
            if not cur.rowcount: return jsonify(error='Not found'),404
        return jsonify(ok=True)

@app.route('/api/my-observations',methods=['GET','POST'])
@app.route('/api/my-observations/<int:item_id>',methods=['PUT','DELETE'])
def my_observations(item_id=None):
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory=sqlite3.Row
        if request.method=='GET':
            return jsonify(items=[dict(r) for r in conn.execute('SELECT * FROM point_observations ORDER BY id').fetchall()])
        if request.method=='DELETE':
            cur=conn.execute('DELETE FROM point_observations WHERE id=?',(item_id,))
            return (jsonify(ok=True) if cur.rowcount else (jsonify(error='Not found'),404))
        try: lat,lon,date,confidence,notes=validate_observation(request.get_json(silent=True))
        except ValueError as e:return jsonify(error=str(e)),400
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        if item_id is None:
            conn.execute('INSERT INTO point_observations(species_id,lat,lon,observed_on,confidence,notes,updated_at) VALUES(?,?,?,?,?,?,?)',(SPECIES['id'],lat,lon,date,confidence,notes,now))
        else:
            cur=conn.execute('UPDATE point_observations SET species_id=?,lat=?,lon=?,observed_on=?,confidence=?,notes=?,updated_at=? WHERE id=?',(SPECIES['id'],lat,lon,date,confidence,notes,now,item_id))
            if not cur.rowcount:return jsonify(error='Not found'),404
        return jsonify(ok=True)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS user_areas (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, habitat TEXT NOT NULL, notes TEXT NOT NULL, geometry TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS point_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, species_id TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, observed_on TEXT NOT NULL, confidence TEXT NOT NULL, notes TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS field_notes (
            field_id TEXT NOT NULL,
            species_id TEXT NOT NULL,
            grazing TEXT NOT NULL,
            access TEXT NOT NULL,
            observation TEXT NOT NULL,
            notes TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(field_id, species_id)
        )""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(field_notes)")}
        if "assessment" not in columns:
            conn.execute("ALTER TABLE field_notes ADD COLUMN assessment TEXT NOT NULL DEFAULT 'unknown'")
        if "visited" not in columns:
            conn.execute("ALTER TABLE field_notes ADD COLUMN visited INTEGER NOT NULL DEFAULT 0")


def get_db_note(field_id, species_id):
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM field_notes WHERE field_id=? AND species_id=?",
            (field_id, species_id)
        ).fetchone()
        return dict(row) if row else None




@app.route('/api/halo-annotations/all')
def all_halo_annotations():
    if request.args.get('species') != SPECIES['id']:
        return jsonify(error='Unknown species'),400
    with DB_LOCK,sqlite3.connect(DB_PATH) as conn:
        rows=conn.execute('SELECT field_id,assessment,grazing FROM field_notes WHERE species_id=?', (SPECIES['id'],)).fetchall()
    return jsonify(notes={str(fid):{'assessment':assessment,'grazing':grazing} for fid,assessment,grazing in rows})

@app.route("/api/halo-annotations", methods=["POST"])
def halo_annotations():
    """Return user exclusions for regional habitat markers, without mutating GIS data."""
    payload=request.get_json(silent=True) or {}
    ids=payload.get('field_ids',[])
    if (payload.get('species_id')!=SPECIES['id'] or not isinstance(ids,list)
        or len(ids)>1500 or any(not isinstance(i,str) or not i.isdigit() for i in ids)):
        return jsonify(error='Invalid halo annotation request'),400
    notes={}
    with DB_LOCK,sqlite3.connect(DB_PATH) as conn:
        for offset in range(0,len(ids),300):
            chunk=ids[offset:offset+300]
            if not chunk:continue
            sql='SELECT field_id,assessment,grazing FROM field_notes WHERE species_id=? AND field_id IN ('+','.join('?' for _ in chunk)+')'
            for field_id,assessment,grazing in conn.execute(sql,[SPECIES['id'],*chunk]):
                notes[field_id]={'assessment':assessment,'grazing':grazing}
    return jsonify(notes=notes)

@app.route("/api/notes/batch", methods=["POST"])
def batch_notes():
    data = request.get_json(silent=True) or {}
    ids = data.get("field_ids", [])
    if (data.get("species_id") != SPECIES["id"] or not isinstance(ids, list)
            or len(ids) > 2500 or any(not isinstance(i, str) or not i.isdigit() for i in ids)):
        return jsonify({"error": "Invalid annotation request"}), 400
    if not ids:
        return jsonify({"notes": {}})
    placeholders = ",".join("?" for _ in ids)
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM field_notes WHERE species_id=? AND field_id IN ({placeholders})",
            [SPECIES["id"], *ids],
        ).fetchall()
    return jsonify({"notes": {r["field_id"]: dict(r) for r in rows}})


@app.route('/api/notes/group',methods=['GET','PUT'])
def group_notes():
    if request.method=='GET':
        ids=request.args.get('ids','').split(',')
        species=request.args.get('species','')
    else:
        payload=request.get_json(silent=True) or {}
        ids=payload.get('field_ids',[])
        species=payload.get('species_id')
    if (species!=SPECIES['id'] or not isinstance(ids,list) or not 1<=len(ids)<=40
        or any(not isinstance(v,str) or not ((v.isdigit() and len(v)<=20) or (v.startswith('custom:') and v[7:].isdigit() and len(v)<=27)) for v in ids)):
        return jsonify(error='Invalid group notes request'),400
    if request.method=='GET':
        notes=[get_db_note(fid,species) for fid in ids]
        notes=[n for n in notes if n]
        notes.sort(key=lambda n:str(n.get('updated_at') or ''),reverse=True)
        return jsonify(note=notes[0] if notes else None,
                       conflicting_saved_notes=len({str((n.get('assessment'),n.get('grazing'))) for n in notes})>1)
    payload=dict(payload)
    payload.pop('field_ids',None)
    # Validate with the existing single-record route and write to every linked ID.
    # Delegate to shared existing handler using temporary Flask request contexts.
    result=None
    for fid in ids:
        with app.test_request_context('/api/notes/'+fid,method='PUT',json=payload):
            result=field_notes(fid)
        if isinstance(result,tuple) and result[1]>=400:return result
    return result


@app.route("/api/notes/<field_id>", methods=["GET", "PUT"])
def field_notes(field_id):
    if not ((field_id.isdigit() and len(field_id)<=20) or (field_id.startswith('custom:') and field_id[7:].isdigit() and len(field_id)<=27)):
        return jsonify({"error": "Invalid field ID"}), 400
    if request.method == "GET":
        species_id = request.args.get("species", "")
        if species_id != SPECIES["id"]:
            return jsonify({"error": "Unknown species"}), 400
        return jsonify({"note": get_db_note(field_id, species_id)})

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("species_id") != SPECIES["id"]:
        return jsonify({"error": "Invalid species or request"}), 400
    allowed = {
        "grazing": {"unknown", "confirmed", "none_observed"},
        "access": {"unknown", "accessible", "permission_required", "denied"},
        "observation": {"not_surveyed", "observed", "not_observed"},
        "assessment": {"unknown", "promising", "unsuitable"},
    }
    if any(data.get(k) not in opts for k, opts in allowed.items()):
        return jsonify({"error": "Invalid selection"}), 400
    if not isinstance(data.get("visited"), bool):
        return jsonify({"error": "Invalid visited status"}), 400
    notes = data.get("notes")
    if not isinstance(notes, str) or len(notes) > 2000:
        return jsonify({"error": "Invalid notes"}), 400
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO field_notes
            (field_id,species_id,grazing,access,observation,notes,updated_at,assessment,visited)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(field_id,species_id) DO UPDATE SET
              grazing=excluded.grazing, access=excluded.access,
              observation=excluded.observation, notes=excluded.notes,
              updated_at=excluded.updated_at,
              assessment=excluded.assessment, visited=excluded.visited""",
            (field_id, SPECIES["id"], data["grazing"], data["access"],
             data["observation"], notes, now, data["assessment"], int(data["visited"])))
    return jsonify({"note": get_db_note(field_id, SPECIES["id"])})


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
