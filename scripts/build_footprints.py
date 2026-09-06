#!/usr/bin/env python3
"""Harvest project footprints / layouts from external spatial sources and
merge them with the document-derived layouts into data/footprints/.

NEEDS INTERNET -> runs in the fetch-footprints.yml lane (the dev sandbox has
no egress). Every source stages raw results under data/raw/footprints/<src>/
so a run can be resumed and re-merged offline.

Sources (in order of authority for overlapping elements)
  rea   Ontario REA equipment schedules  -- scripts/extract_rea_layouts.py
        (offline, run first; this script only merges its output)
  gis   Geospatial files filed with the registries: IAAC "Supporting
        geospatial data" attachments (zip of shapefiles/KML filed with every
        IAA 2019 project description), plus any KML/KMZ/SHP/GeoJSON link in a
        catalogue (queue = data/raw/map_docs_index.json gis_queue +
        gis_data docs). Pure-python shapefile + KML readers; UTM .prj handled
        natively, other projections via pyproj when installed.
  cwtd  Canadian Wind Turbine Database (NRCan, open.canada.ca): every turbine
        in Canada with project name, model, hub height, rotor, capacity.
        Matched to wind projects by name tokens + distance.
  osm   OpenStreetMap via Overpass, per project point, radius by sector:
        pits, tailings, reservoirs, dams, plants, substations, lines,
        pipelines, wells, shafts. Name-matched elements are 'high'
        confidence; unnamed ones are kept only when their type fits the
        project's sector and they sit close to the pin ('proximity').

Usage
  build_footprints.py [--source all|gis|cwtd|osm] [--budget SEC] [--limit N]
                      [--merge-only] [--cwtd-file CSV] [--osm-fixture JSON]
Then: build_national_geojson.py -> gap_reconcile.py -> build_api.py
"""
import argparse
import csv
import gzip
import io
import json
import math
import os
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from footprints_common import (FOOTPRINT_DIR, ROOT, ROLES, fold, haversine_km, in_canada,  # noqa: E402
                               iter_coords, load_footprint, load_index, make_id, save_index,
                               utm_to_latlon, write_footprint)

RAW = os.path.join(ROOT, 'data', 'raw', 'footprints')
GEOJSON = os.path.join(ROOT, 'data', 'projects_canada.geojson')
MAP_DOCS = os.path.join(ROOT, 'data', 'raw', 'map_docs_index.json')
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
MAX_BYTES = 80_000_000
STOP = {'wind', 'energy', 'centre', 'center', 'project', 'projet', 'farm', 'park', 'parc', 'power',
        'phase', 'inc', 'lp', 'l.p.', 'the', 'of', 'and', 'ltd', 'limited', 'ontario', 'alberta',
        'quebec', 'eolien', 'eoliennes', 'eolienne', 'des', 'de', 'la', 'le', 'du', 'les', 'ii', 'iii',
        'i', 'expansion', 'extension', 'facility', 'generating', 'station', 'mine', 'mining',
        'solar', 'corporation', 'corp', 'company', 'co', 'canada', 'hydro', 'development', 'ea'}


def log(*a):
    print(*a, flush=True)


def tokens(name):
    return {t for t in re.split(r'[^a-z0-9]+', fold(name)) if len(t) > 2 and t not in STOP}


# ── HTTP ─────────────────────────────────────────────────────────────
class Budget:
    def __init__(self, seconds):
        self.t0 = time.time()
        self.seconds = seconds

    def left(self):
        return self.seconds - (time.time() - self.t0)

    def ok(self):
        return self.left() > 0


def fetch(url, timeout=120, data=None, headers=None, tries=3):
    h = {'User-Agent': UA, 'Accept': '*/*', 'Accept-Language': 'en-CA,en;q=0.9,fr-CA;q=0.8'}
    h.update(headers or {})
    last = None
    for t in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                clen = r.headers.get('Content-Length')
                if clen and int(clen) > MAX_BYTES:
                    raise ValueError(f'too large ({int(clen) // 1_000_000} MB)')
                blob = r.read(MAX_BYTES + 1)
                if len(blob) > MAX_BYTES:
                    raise ValueError('too large (streamed)')
                return blob, (r.headers.get('Content-Type') or '').split(';')[0], r.geturl()
        except Exception as e:  # noqa: BLE001
            last = e
            if 'too large' in str(e):
                break
            time.sleep(2 ** (t + 1))
    raise last


# ── project index ────────────────────────────────────────────────────
def load_projects():
    d = json.load(open(GEOJSON))
    out = []
    for f in d['features']:
        p = f['properties']
        if p.get('source') in ('on_amis', 'nrcan_gap'):
            continue
        pid = make_id(p['jurisdiction'], p['name'])
        ll = None
        if f.get('geometry') and f['geometry'].get('coordinates'):
            lon, lat = f['geometry']['coordinates']
            ll = (lat, lon)
        out.append({'pid': pid, 'name': p['name'], 'jurisdiction': p['jurisdiction'],
                    'category': p.get('category') or 'other', 'type': p.get('type') or '',
                    'proponent': p.get('proponent') or '', 'll': ll,
                    'docs': p.get('doc_count') or 0, 'approx': p.get('geocode') == 'approximate',
                    'tokens': tokens(p['name'])})
    return out


def stage_path(src, pid):
    os.makedirs(os.path.join(RAW, src), exist_ok=True)
    return os.path.join(RAW, src, f'{pid}.json')


def stage_write(src, pid, features, meta):
    json.dump({'type': 'FeatureCollection', 'features': features,
               'properties': {'fetched': time.strftime('%Y-%m-%d'), **meta}},
              open(stage_path(src, pid), 'w'), ensure_ascii=False, separators=(',', ':'))


def stage_read(src, pid):
    p = stage_path(src, pid)
    return json.load(open(p)) if os.path.exists(p) else None


# ── GIS files: KML / shapefile / GeoJSON ─────────────────────────────
FED_FILE_RX = re.compile(r'href=["\']?(/050/documents/[^"\'\s>]+)', re.I)
ROLE_FROM_NAME = [
    ('tailings', re.compile(r'tailing|\btsf\b|\btmf\b|\btia\b', re.I)),
    ('pit', re.compile(r'\bpit|quarry|open.?cast|carri[eè]re|fosse', re.I)),
    ('waste_rock', re.compile(r'waste.?rock|stockpile|overburden|halde|st[ée]rile', re.I)),
    ('transmission_line', re.compile(r'transmission|power.?line|\bkv\b|ligne', re.I)),
    ('collector_line', re.compile(r'collector|distribution', re.I)),
    ('pipeline', re.compile(r'pipeline|pipe|gazoduc|ol[ée]oduc', re.I)),
    ('access_road', re.compile(r'\broad|route|chemin|haul|access', re.I)),
    ('rail', re.compile(r'\brail|railway|spur|voie ferr', re.I)),
    ('turbine', re.compile(r'turbine|\bwtg|\bt\d{1,3}\b|[ée]olienne', re.I)),
    ('well', re.compile(r'\bwell|wellpad|puits|borehole|drill', re.I)),
    ('dam', re.compile(r'\bdam\b|barrage|dike|dyke|digue|berm', re.I)),
    ('reservoir', re.compile(r'reservoir|headpond|impound|bief|pond', re.I)),
    ('penstock', re.compile(r'penstock|canal|tunnel|conduite|intake|tailrace', re.I)),
    ('powerhouse', re.compile(r'powerhouse|centrale', re.I)),
    ('substation', re.compile(r'substation|switchyard|poste', re.I)),
    ('solar_array', re.compile(r'solar|array|panel|photovolta', re.I)),
    ('plant', re.compile(r'plant|mill|process|facility|usine|terminal|refinery|smelter|concentrator|camp', re.I)),
    ('landfill', re.compile(r'landfill|disposal|cell|d[ée]charge|lieu d.enfouissement', re.I)),
    ('building', re.compile(r'building|b[âa]timent|warehouse|office|shop', re.I)),
    ('footprint', re.compile(r'footprint|boundary|boundaries|\bpda\b|project.?area|site|lease|claim|permit|'
                             r'disturbance|development.?area|zone|emprise|p[ée]rim[eè]tre|limite|study.?area|'
                             r'\blsa\b|\brsa\b|property|tenure|licen[cs]e', re.I)),
]


def role_from_name(name, geom_type):
    for role, rx in ROLE_FROM_NAME:
        if rx.search(name or ''):
            if role == 'turbine' and geom_type != 'Point':
                continue
            if role in ('transmission_line', 'pipeline', 'access_road', 'rail', 'penstock',
                        'collector_line') and geom_type == 'Point':
                continue
            return role
    return {'Point': 'other', 'MultiPoint': 'other', 'LineString': 'other', 'MultiLineString': 'other'}.get(
        geom_type, 'footprint')


def parse_kml(blob, layer_name):
    """KML -> features (lon/lat already geographic)."""
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return []
    ns = re.match(r'\{.*\}', root.tag)
    ns = ns.group(0) if ns else ''
    feats = []

    def coords_of(el):
        out = []
        for c in el.iter(ns + 'coordinates'):
            pts = []
            for tok in (c.text or '').split():
                parts = tok.split(',')
                if len(parts) >= 2:
                    try:
                        pts.append([float(parts[0]), float(parts[1])])
                    except ValueError:
                        pass
            if pts:
                out.append(pts)
        return out

    for pm in root.iter(ns + 'Placemark'):
        name = (pm.findtext(ns + 'name') or '').strip()
        desc = (pm.findtext(ns + 'description') or '')[:200]
        for tag, gtype in (('Point', 'Point'), ('LineString', 'LineString'), ('Polygon', 'Polygon')):
            for g in pm.iter(ns + tag):
                if gtype == 'Polygon':
                    outer = g.find(f'{ns}outerBoundaryIs')
                    rings = coords_of(outer) if outer is not None else coords_of(g)
                    inner = [r for ib in g.findall(f'{ns}innerBoundaryIs') for r in coords_of(ib)]
                    if not rings:
                        continue
                    geom = {'type': 'Polygon', 'coordinates': [rings[0]] + inner}
                else:
                    rings = coords_of(g)
                    if not rings:
                        continue
                    geom = {'type': gtype, 'coordinates': rings[0][0] if gtype == 'Point' else rings[0]}
                feats.append({'type': 'Feature', 'geometry': geom,
                              'properties': {'name': name or None, 'layer': layer_name,
                                             'role': role_from_name(f'{layer_name} {name} {desc}', gtype)}})
    return feats


def read_shp(blob):
    """Minimal ESRI shapefile reader -> list of (geom_type, parts[[x,y],...])."""
    if len(blob) < 100:
        return []
    out = []
    pos = 100
    n = len(blob)
    while pos + 8 <= n:
        (clen,) = struct.unpack('>i', blob[pos + 4:pos + 8])
        rec = blob[pos + 8:pos + 8 + clen * 2]
        pos += 8 + clen * 2
        if len(rec) < 4:
            continue
        (stype,) = struct.unpack('<i', rec[:4])
        base = stype % 10 if stype in (11, 13, 15, 18, 21, 23, 25, 28) else stype
        try:
            if base == 1:
                x, y = struct.unpack('<2d', rec[4:20])
                out.append(('Point', [[[x, y]]]))
            elif base in (3, 5):
                nparts, npts = struct.unpack('<2i', rec[36:44])
                parts = struct.unpack(f'<{nparts}i', rec[44:44 + 4 * nparts])
                off = 44 + 4 * nparts
                pts = struct.unpack(f'<{2 * npts}d', rec[off:off + 16 * npts])
                pts = [[pts[i], pts[i + 1]] for i in range(0, len(pts), 2)]
                rings = [pts[parts[i]:(parts[i + 1] if i + 1 < nparts else npts)] for i in range(nparts)]
                out.append(('Polygon' if base == 5 else 'LineString', rings))
            elif base == 8:
                (npts,) = struct.unpack('<i', rec[36:40])
                pts = struct.unpack(f'<{2 * npts}d', rec[40:40 + 16 * npts])
                out.append(('MultiPoint', [[[pts[i], pts[i + 1]] for i in range(0, len(pts), 2)]]))
        except struct.error:
            continue
    return out


def read_dbf_names(blob):
    """First text field of each .dbf record (layer attribute used as a name)."""
    try:
        nrec = struct.unpack('<I', blob[4:8])[0]
        hlen, rlen = struct.unpack('<2H', blob[8:12])
        fields = []
        p = 32
        while p + 32 <= hlen and blob[p] != 0x0D:
            name = blob[p:p + 11].split(b'\0')[0].decode('latin-1')
            ftype = chr(blob[p + 11])
            flen = blob[p + 16]
            fields.append((name, ftype, flen))
            p += 32
        names = []
        for i in range(nrec):
            rec = blob[hlen + i * rlen: hlen + (i + 1) * rlen]
            off = 1
            best = ''
            for name, ftype, flen in fields:
                val = rec[off:off + flen].decode('latin-1', 'replace').strip()
                off += flen
                if ftype == 'C' and val and re.search(r'name|type|class|desc|feature|label|id', name, re.I):
                    best = f'{best} {val}'.strip() if best else val
            names.append(best[:120])
        return names
    except Exception:  # noqa: BLE001
        return []


def prj_transform(prj_text):
    """Return fn(x, y) -> (lon, lat) for the .prj, or None if unsupported."""
    t = prj_text or ''
    if not t or re.match(r'\s*GEOGCS', t) or ('GEOGCS' in t and 'PROJCS' not in t):
        return lambda x, y: (x, y)
    m = re.search(r'UTM[_ ]Zone[_ ](\d{1,2})', t, re.I)
    if m:
        zone = int(m.group(1))
        return lambda x, y: utm_to_latlon(x, y, zone)[::-1]
    m = re.search(r'central_meridian"?,\s*(-?\d+(?:\.\d+)?)', t, re.I)
    if m and 'Transverse_Mercator' in t and re.search(r'scale_factor"?,\s*0\.9996', t, re.I):
        zone = int((float(m.group(1)) + 183) // 6)
        return lambda x, y: utm_to_latlon(x, y, zone)[::-1]
    try:
        from pyproj import CRS, Transformer
        tr = Transformer.from_crs(CRS.from_wkt(t), 'EPSG:4326', always_xy=True)
        return lambda x, y: tr.transform(x, y)
    except Exception:  # noqa: BLE001
        return None


def features_from_archive(blob, label, notes):
    """zip / kmz / kml / geojson bytes -> features (WGS84)."""
    feats = []
    head = blob[:4]
    if head[:2] == b'PK':
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            return feats
        names = z.namelist()
        for nm in names:
            low = nm.lower()
            if low.endswith('.kml'):
                feats += parse_kml(z.read(nm), os.path.basename(nm))
            elif low.endswith('.kmz'):
                feats += features_from_archive(z.read(nm), nm, notes)
            elif low.endswith('.geojson') or low.endswith('.json'):
                try:
                    j = json.loads(z.read(nm))
                    for f in (j.get('features') or []):
                        if f.get('geometry'):
                            props = f.get('properties') or {}
                            nmv = next((str(v) for k, v in props.items()
                                        if re.search(r'name|type|class|desc|label', k, re.I) and v), None)
                            feats.append({'type': 'Feature', 'geometry': f['geometry'],
                                          'properties': {'name': nmv, 'layer': os.path.basename(nm),
                                                         'role': role_from_name(f'{nm} {nmv or ""}', f['geometry']['type'])}})
                except Exception:  # noqa: BLE001
                    pass
            elif low.endswith('.shp'):
                base = nm[:-4]
                prj = next((z.read(x).decode('latin-1', 'replace') for x in names if x.lower() == base.lower() + '.prj'), '')
                tr = prj_transform(prj)
                if tr is None:
                    notes.append(f'{os.path.basename(nm)}: unsupported projection (install pyproj)')
                    continue
                dbf = next((z.read(x) for x in names if x.lower() == base.lower() + '.dbf'), b'')
                attr_names = read_dbf_names(dbf) if dbf else []
                shapes = read_shp(z.read(nm))
                for i, (gtype, parts) in enumerate(shapes[:5000]):
                    try:
                        conv = [[list(tr(x, y)) for x, y in ring] for ring in parts]
                    except Exception:  # noqa: BLE001
                        continue
                    if gtype == 'Point':
                        geom = {'type': 'Point', 'coordinates': conv[0][0]}
                    elif gtype == 'MultiPoint':
                        geom = {'type': 'MultiPoint', 'coordinates': conv[0]}
                    elif gtype == 'LineString':
                        geom = {'type': 'MultiLineString', 'coordinates': conv} if len(conv) > 1 else \
                            {'type': 'LineString', 'coordinates': conv[0]}
                    else:
                        geom = {'type': 'Polygon', 'coordinates': conv}   # rings incl. holes
                    an = attr_names[i] if i < len(attr_names) else ''
                    feats.append({'type': 'Feature', 'geometry': geom,
                                  'properties': {'name': an or None, 'layer': os.path.basename(nm),
                                                 'role': role_from_name(f'{base} {an}', geom['type'])}})
    elif b'<kml' in blob[:2000] or b'<Document' in blob[:2000]:
        feats += parse_kml(blob, label)
    elif head.strip()[:1] == b'{':
        try:
            j = json.loads(blob)
            for f in (j.get('features') or []):
                if f.get('geometry'):
                    feats.append({'type': 'Feature', 'geometry': f['geometry'],
                                  'properties': {'layer': label,
                                                 'role': role_from_name(label, f['geometry']['type'])}})
        except Exception:  # noqa: BLE001
            pass
    # sanity: geographic coordinates inside Canada only
    good = []
    for f in feats:
        ok = True
        for lon, lat in iter_coords(f['geometry']):
            if not in_canada(lat, lon):
                ok = False
                break
        if ok:
            good.append(f)
    if len(good) < len(feats):
        notes.append(f'{len(feats) - len(good)} features dropped (not geographic WGS84 / outside Canada)')
    return good


def gis_queue(projects):
    """(pid, url, title) for every geospatial attachment we know of."""
    if not os.path.exists(MAP_DOCS):
        return []
    md = json.load(open(MAP_DOCS))
    seen, q = set(), []
    for pid, e in md['projects'].items():
        for d in e['docs']:
            if d['kind'] != 'gis_data' or not d.get('url') or d['url'] in seen:
                continue
            seen.add(d['url'])
            q.append((pid, d['url'], d.get('title') or ''))
    return q


def run_gis(projects, budget, limit):
    by_pid = {p['pid']: p for p in projects}
    q = gis_queue(projects)
    log(f'gis: {len(q)} geospatial attachments queued')
    state_path = os.path.join(RAW, 'gis_state.json')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    done = 0
    for pid, url, title in q:
        if not budget.ok() or done >= limit:
            break
        st = state.get(url)
        if st and (st.get('status') == 'ok' or st.get('tries', 0) >= 3):
            continue
        proj = by_pid.get(pid)
        if not proj:
            continue
        notes = []
        try:
            blob, ctype, final = fetch(url)
            if 'html' in ctype and b'/050/documents/' in blob:
                m = FED_FILE_RX.search(blob.decode('utf-8', 'replace'))
                if not m:
                    raise ValueError('landing page without attachment')
                furl = urllib.parse.urljoin(url, m.group(1))
                blob, ctype, final = fetch(furl)
            feats = features_from_archive(blob, title, notes)
            for f in feats:
                f['properties']['source'] = 'gis'
                f['properties']['source_doc'] = url
            state[url] = {'status': 'ok' if feats else 'empty', 'n': len(feats), 'file': final,
                          'fetched': time.strftime('%Y-%m-%d'), 'notes': notes[:3]}
            if feats:
                existing = stage_read('gis', pid)
                prev = [f for f in (existing or {}).get('features', []) if f['properties'].get('source_doc') != url]
                stage_write('gis', pid, prev + feats, {
                    'sources': ((existing or {}).get('properties', {}).get('sources') or []) +
                               [{'source': 'gis', 'url': url, 'title': title, 'file': final, 'n': len(feats)}],
                    'notes': notes})
            log(f'  gis {pid} {len(feats):5d} feats  {title[:50]}  {" ".join(notes)[:80]}')
        except Exception as e:  # noqa: BLE001
            state[url] = {'status': 'error', 'error': str(e)[:160], 'tries': (st or {}).get('tries', 0) + 1,
                          'fetched': time.strftime('%Y-%m-%d')}
            log(f'  gis {pid} FAIL {str(e)[:80]}  {url}')
        done += 1
        time.sleep(1.5)
    os.makedirs(RAW, exist_ok=True)
    json.dump(state, open(state_path, 'w'), indent=0)
    log(f'gis: processed {done}; state {Counter(v.get("status") for v in state.values())}')


# ── Canadian Wind Turbine Database ───────────────────────────────────
CKAN = 'https://open.canada.ca/data/api/3/action'
CWTD_FALLBACKS = [
    'https://ftp.maps.canada.ca/pub/nrcan_rncan/Wind-energy_Energie-eolienne/wind_turbines_database/wind_turbine_database_en.csv',
]


def cwtd_download():
    urls = []
    try:
        q = urllib.parse.quote('canadian wind turbine database')
        blob, _, _ = fetch(f'{CKAN}/package_search?q={q}&rows=10')
        for pkg in json.loads(blob)['result']['results']:
            if 'wind turbine' not in (pkg.get('title') or '').lower():
                continue
            for r in pkg.get('resources') or []:
                fmt = (r.get('format') or '').upper()
                if fmt in ('CSV', 'GEOJSON') and r.get('url'):
                    urls.append((0 if fmt == 'CSV' else 1, r['url']))
    except Exception as e:  # noqa: BLE001
        log('  cwtd CKAN discovery failed:', str(e)[:80])
    urls = [u for _, u in sorted(urls)] + CWTD_FALLBACKS
    for u in urls:
        try:
            blob, ctype, final = fetch(u)
            log(f'  cwtd: {len(blob) // 1000} kB from {final}')
            return blob, final
        except Exception as e:  # noqa: BLE001
            log('  cwtd fetch failed', u[:80], str(e)[:60])
    return None, None


def cwtd_rows(blob):
    txt = blob.decode('utf-8-sig', 'replace')
    if txt.lstrip().startswith('{'):
        j = json.loads(txt)
        rows = []
        for f in j.get('features') or []:
            p = dict(f.get('properties') or {})
            if f.get('geometry') and f['geometry'].get('coordinates'):
                p.setdefault('Longitude', f['geometry']['coordinates'][0])
                p.setdefault('Latitude', f['geometry']['coordinates'][1])
            rows.append(p)
        return rows
    return list(csv.DictReader(io.StringIO(txt)))


def pick(keys, *wants):
    for w in wants:
        for k in keys:
            if w in k.lower().replace('_', ' '):
                return k
    return None


def cwtd_normalize(rows):
    if not rows:
        return []
    keys = list(rows[0].keys())
    k = {
        'project': pick(keys, 'project name', 'project', 'nom du projet'),
        'tid': pick(keys, 'turbine identifier', 'turbine id', 'identifier'),
        'tnum': pick(keys, 'turbine number'),
        'kw': pick(keys, 'rated capacity', 'turbine rated', 'capacity (kw)'),
        'mw': pick(keys, 'total project capacity', 'project capacity'),
        'rotor': pick(keys, 'rotor diameter', 'rotor'),
        'hub': pick(keys, 'hub height', 'hub'),
        'manu': pick(keys, 'manufacturer'),
        'model': pick(keys, 'model'),
        'date': pick(keys, 'commissioning', 'commission'),
        'prov': pick(keys, 'province'),
        'lat': pick(keys, 'latitude', 'lat'),
        'lon': pick(keys, 'longitude', 'long', 'lon'),
    }
    log('  cwtd columns:', {a: b for a, b in k.items() if b})
    out = []
    for r in rows:
        try:
            lat, lon = float(r[k['lat']]), float(r[k['lon']])
        except Exception:  # noqa: BLE001
            continue
        if not in_canada(lat, lon):
            continue

        def num(key):
            v = r.get(k[key]) if k[key] else None
            try:
                return float(str(v).replace(',', '')) if v not in (None, '') else None
            except ValueError:
                return None
        out.append({'project': (r.get(k['project']) or '').strip() if k['project'] else '',
                    'label': (r.get(k['tid']) or r.get(k['tnum']) or '') if (k['tid'] or k['tnum']) else '',
                    'kw': num('kw'), 'mw': num('mw'), 'rotor': num('rotor'), 'hub': num('hub'),
                    'manufacturer': (r.get(k['manu']) or '').strip() if k['manu'] else '',
                    'model': (r.get(k['model']) or '').strip() if k['model'] else '',
                    'date': (r.get(k['date']) or '').strip() if k['date'] else '',
                    'prov': (r.get(k['prov']) or '').strip() if k['prov'] else '',
                    'lat': lat, 'lon': lon})
    return out


def cwtd_match(turbines, projects):
    """Group turbines by CWTD project, match each group to map features."""
    groups = defaultdict(list)
    for t in turbines:
        groups[t['project'] or f'_{round(t["lat"], 2)},{round(t["lon"], 2)}'].append(t)
    wind = [p for p in projects if p['ll'] and (p['category'] == 'wind' or re.search(r'wind|eolien|éolien', fold(p['name'] + ' ' + p['type']), re.I))]
    log(f'  cwtd: {len(turbines)} turbines in {len(groups)} projects; {len(wind)} wind features on the map')
    matched = defaultdict(list)   # pid -> [(group_name, turbines, how)]
    n_groups = 0
    for gname, ts in groups.items():
        clat = sum(t['lat'] for t in ts) / len(ts)
        clon = sum(t['lon'] for t in ts) / len(ts)
        gt = tokens(gname)
        hits = []
        for p in wind:
            d = haversine_km(clat, clon, p['ll'][0], p['ll'][1])
            if d > 40:
                continue
            ov = len(gt & p['tokens'])
            if (ov >= 1 and d <= 30) or (d <= 4 and (ov or p['approx'] is False)) or (ov >= 2):
                hits.append((0 if ov else 1, d, p, 'name' if ov else 'proximity'))
        if not hits:
            continue
        hits.sort(key=lambda h: (h[0], h[1]))
        # every registry record of the same wind farm gets the layout, but a
        # proximity-only hit is accepted only when nothing name-matched
        best_kind = hits[0][3]
        for _, d, p, how in hits:
            if how == 'proximity' and best_kind == 'name':
                continue
            if how == 'proximity' and d > 4:
                continue
            matched[p['pid']].append((gname, ts, how, round(d, 1)))
        n_groups += 1
    log(f'  cwtd: {n_groups} turbine groups matched -> {len(matched)} map features')
    return matched


def run_cwtd(projects, cwtd_file=None):
    if cwtd_file:
        blob, src = open(cwtd_file, 'rb').read(), cwtd_file
    else:
        blob, src = cwtd_download()
    if not blob:
        log('cwtd: no data'); return
    os.makedirs(os.path.join(RAW, 'cwtd'), exist_ok=True)
    with gzip.open(os.path.join(RAW, 'cwtd', 'cwtd_raw.gz'), 'wb') as fh:
        fh.write(blob)
    turbines = cwtd_normalize(cwtd_rows(blob))
    json.dump({'source': src, 'fetched': time.strftime('%Y-%m-%d'), 'n': len(turbines)},
              open(os.path.join(RAW, 'cwtd', 'meta.json'), 'w'))
    matched = cwtd_match(turbines, projects)
    for pid, items in matched.items():
        feats, srcs = [], []
        for gname, ts, how, d in items:
            for t in ts:
                props = {'role': 'turbine', 'label': str(t['label']) or None, 'source': 'cwtd',
                         'name': gname, 'match': how}
                for a, b in (('kw', 'capacity_kw'), ('hub', 'hub_height_m'), ('rotor', 'rotor_diameter_m')):
                    if t.get(a):
                        props[b] = t[a]
                desc = ' '.join(x for x in (t.get('manufacturer'), t.get('model')) if x)
                if desc:
                    props['description'] = desc
                if t.get('date'):
                    props['commissioned'] = t['date']
                feats.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [t['lon'], t['lat']]},
                              'properties': props})
            srcs.append({'source': 'cwtd', 'title': f'Canadian Wind Turbine Database: {gname}', 'n': len(ts),
                         'match': how, 'distance_km': d, 'url': 'https://open.canada.ca/data/en/dataset/79fdad93-9025-49ad-ba16-c26d718cc070'})
        stage_write('cwtd', pid, feats, {'sources': srcs,
                                          'confidence': 'high' if all(i[2] == 'name' for i in items) else 'medium'})
    log(f'cwtd: staged {len(matched)} projects')


# ── OpenStreetMap / Overpass ─────────────────────────────────────────
OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter']
OSM_RADIUS_KM = {'mining': 5, 'hydro': 6, 'solar': 3, 'wind': 8, 'energy_other': 5, 'oil_gas': 5, 'transmission': 10,
                 'nuclear': 4, 'waste': 3, 'industrial': 3, 'biogas': 2}
SECTOR_ROLES = {
    'mining': {'pit', 'tailings', 'waste_rock', 'shaft', 'plant', 'rail', 'access_road'},
    'hydro': {'dam', 'reservoir', 'powerhouse', 'penstock', 'plant', 'substation', 'transmission_line'},
    'solar': {'solar_array', 'substation', 'plant'},
    'wind': {'turbine', 'substation', 'collector_line'},
    'energy_other': {'transmission_line', 'substation', 'plant', 'generator', 'pipeline'},
    'transmission': {'transmission_line', 'collector_line', 'substation'},
    'oil_gas': {'well', 'pipeline', 'plant', 'landfill'},
    'nuclear': {'plant', 'substation', 'transmission_line', 'building'},
    'waste': {'landfill', 'plant'},
    'industrial': {'plant', 'building', 'rail', 'pipeline'},
    'biogas': {'plant', 'generator'},
}
OSM_QUERY = '''[out:json][timeout:60];
(
  way(around:{r},{lat},{lon})[landuse~"^(quarry|industrial|reservoir|landfill)$"];
  relation(around:{r},{lat},{lon})[landuse~"^(quarry|industrial|reservoir|landfill)$"];
  way(around:{r},{lat},{lon})[man_made~"^(tailings_pond|mineshaft|adit|works|pipeline|dam|dyke)$"];
  node(around:{r},{lat},{lon})[man_made~"^(mineshaft|adit|petroleum_well)$"];
  way(around:{r},{lat},{lon})[waterway~"^(dam|weir|canal)$"];
  way(around:{r},{lat},{lon})[water=reservoir];
  relation(around:{r},{lat},{lon})[water=reservoir];
  way(around:{r},{lat},{lon})[power~"^(plant|substation|line|minor_line|generator)$"];
  relation(around:{r},{lat},{lon})[power~"^(plant|substation)$"];
  node(around:{r},{lat},{lon})[power~"^(generator|substation)$"];
  way(around:{r},{lat},{lon})[industrial~"^(mine|oil|gas|refinery|factory|port)$"];
);
out tags geom;'''


def osm_role(tags):
    t = tags
    if t.get('landuse') == 'quarry':
        return 'pit'
    if t.get('man_made') == 'tailings_pond':
        return 'tailings'
    if t.get('landuse') == 'reservoir' or t.get('water') == 'reservoir':
        return 'reservoir'
    if t.get('waterway') in ('dam', 'weir') or t.get('man_made') in ('dam', 'dyke'):
        return 'dam'
    if t.get('waterway') == 'canal':
        return 'penstock'
    if t.get('man_made') in ('mineshaft', 'adit'):
        return 'shaft'
    if t.get('man_made') == 'petroleum_well':
        return 'well'
    if t.get('man_made') == 'pipeline':
        return 'pipeline'
    if t.get('power') == 'line':
        return 'transmission_line'
    if t.get('power') == 'minor_line':
        return 'collector_line'
    if t.get('power') == 'substation':
        return 'substation'
    if t.get('power') == 'generator':
        src = t.get('generator:source', '')
        if src == 'wind':
            return 'turbine'
        if src == 'solar':
            return 'solar_array'
        if src == 'hydro':
            return 'powerhouse'
        return 'generator'
    if t.get('power') == 'plant':
        if t.get('plant:source') == 'solar':
            return 'solar_array'
        return 'plant'
    if t.get('landuse') == 'landfill':
        return 'landfill'
    if t.get('landuse') == 'industrial' or t.get('man_made') == 'works' or t.get('industrial'):
        return 'plant'
    return 'other'


def osm_geometry(el):
    typ = el.get('type')
    if typ == 'node':
        return {'type': 'Point', 'coordinates': [el['lon'], el['lat']]}
    if typ == 'way':
        pts = [[p['lon'], p['lat']] for p in el.get('geometry') or []]
        if len(pts) < 2:
            return None
        tags = el.get('tags') or {}
        closed = pts[0] == pts[-1] and len(pts) >= 4
        area_like = tags.get('landuse') or tags.get('water') or tags.get('power') in ('plant', 'substation') \
            or tags.get('man_made') in ('tailings_pond', 'works') or tags.get('industrial') or tags.get('area') == 'yes'
        if closed and area_like:
            return {'type': 'Polygon', 'coordinates': [pts]}
        return {'type': 'LineString', 'coordinates': pts}
    if typ == 'relation':
        outers, inners = [], []
        for m in el.get('members') or []:
            if m.get('type') != 'way' or not m.get('geometry'):
                continue
            ring = [[p['lon'], p['lat']] for p in m['geometry']]
            if len(ring) >= 4 and ring[0] == ring[-1]:
                (inners if m.get('role') == 'inner' else outers).append(ring)
        if not outers:
            return None
        if len(outers) == 1:
            return {'type': 'Polygon', 'coordinates': [outers[0]] + inners}
        return {'type': 'MultiPolygon', 'coordinates': [[o] for o in outers]}
    return None


def osm_query(lat, lon, r_m):
    q = OSM_QUERY.format(r=int(r_m), lat=lat, lon=lon)
    data = urllib.parse.urlencode({'data': q}).encode()
    last = None
    for ep in OVERPASS:
        try:
            blob, _, _ = fetch(ep, data=data, timeout=90, tries=2)
            return json.loads(blob)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10)
    raise last


def osm_candidates(projects):
    cands = [p for p in projects if p['ll'] and not p['approx'] and p['category'] in OSM_RADIUS_KM]
    # documented projects first, then big-footprint sectors
    order = {'mining': 0, 'hydro': 1, 'oil_gas': 2, 'nuclear': 3, 'energy_other': 4, 'wind': 5,
             'solar': 6, 'industrial': 7, 'waste': 8, 'biogas': 9, 'transmission': 4}
    cands.sort(key=lambda p: (-min(p['docs'], 50), order.get(p['category'], 9), p['name']))
    return cands


def run_osm(projects, budget, limit, fixture=None):
    state_path = os.path.join(RAW, 'osm_state.json')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    cands = osm_candidates(projects)
    log(f'osm: {len(cands)} candidate projects; {len(state)} already probed')
    done = kept = 0
    today = time.strftime('%Y-%m-%d')
    for p in cands:
        if done >= limit or not budget.ok():
            break
        st = state.get(p['pid'])
        if st and (today[:4] == (st.get('fetched') or '')[:4]) and not fixture:
            continue
        r_km = OSM_RADIUS_KM[p['category']]
        try:
            j = fixture if fixture else osm_query(p['ll'][0], p['ll'][1], r_km * 1000)
        except Exception as e:  # noqa: BLE001
            log(f'  osm {p["pid"]} FAIL {str(e)[:80]}')
            state[p['pid']] = {'fetched': today, 'status': 'error', 'error': str(e)[:120]}
            done += 1
            time.sleep(15)
            continue
        ptoks = p['tokens'] | tokens(p['proponent'])
        feats = []
        for el in j.get('elements') or []:
            tags = el.get('tags') or {}
            geom = osm_geometry(el)
            if not geom:
                continue
            role = osm_role(tags)
            if role == 'other':
                continue
            nm = tags.get('name') or tags.get('operator') or ''
            ov = tokens(nm) & ptoks if nm else set()
            # nearest vertex distance to the pin
            dmin = min(haversine_km(lat, lon, p['ll'][0], p['ll'][1]) for lon, lat in iter_coords(geom))
            if ov:
                how = 'name'
            elif role in SECTOR_ROLES.get(p['category'], set()) and dmin <= r_km / 2:
                how = 'proximity'
            else:
                continue
            props = {'role': role, 'source': 'osm', 'match': how,
                     'osm_id': f"{el['type']}/{el['id']}"}
            if nm:
                props['name'] = nm
            for k in ('operator', 'voltage', 'generator:output:electricity', 'resource', 'start_date'):
                if tags.get(k):
                    props[k.replace(':', '_')] = tags[k]
            feats.append({'type': 'Feature', 'geometry': geom, 'properties': props})
        # a proximity-only result made of nothing but lines/substations is
        # usually the grid passing by -> demand at least one area/point element
        if feats and p['category'] != 'transmission' and all(f['properties']['match'] == 'proximity' for f in feats):
            kinds = {f['properties']['role'] for f in feats}
            if kinds <= {'transmission_line', 'collector_line', 'substation', 'pipeline', 'access_road', 'rail'}:
                feats = []
        conf = 'high' if any(f['properties']['match'] == 'name' for f in feats) else 'medium'
        state[p['pid']] = {'fetched': today, 'status': 'ok', 'n': len(feats), 'conf': conf}
        if feats:
            stage_write('osm', p['pid'], feats, {
                'sources': [{'source': 'osm', 'title': 'OpenStreetMap (Overpass API)',
                             'url': 'https://www.openstreetmap.org/', 'n': len(feats),
                             'radius_km': r_km}],
                'confidence': conf, 'license': 'ODbL — © OpenStreetMap contributors'})
            kept += 1
            log(f'  osm {p["pid"]} {p["category"]:12} {len(feats):3d} elements ({conf})  {p["name"][:50]}')
        done += 1
        if not fixture:
            time.sleep(3)
        if done % 25 == 0:
            json.dump(state, open(state_path, 'w'), indent=0)
    os.makedirs(RAW, exist_ok=True)
    json.dump(state, open(state_path, 'w'), indent=0)
    log(f'osm: probed {done}, layouts for {kept}')


# ── merge ────────────────────────────────────────────────────────────
SOURCE_LABEL = {'ontario_rea': 'Ontario REA equipment schedule', 'gis': 'registry geospatial data',
                'cwtd': 'Canadian Wind Turbine Database', 'osm': 'OpenStreetMap'}
CONF_RANK = {'high': 0, 'medium': 1, 'low': 2}


def merge_all(projects):
    by_pid = {p['pid']: p for p in projects}
    index = load_index()
    pids = set(index)
    for src in ('gis', 'cwtd', 'osm'):
        d = os.path.join(RAW, src)
        if os.path.isdir(d):
            pids |= {fn[:-5] for fn in os.listdir(d) if fn.endswith('.json')}
    n_written = 0
    for pid in sorted(pids):
        existing = load_footprint(pid) or {}
        rea = [f for f in existing.get('features', []) if f['properties'].get('source') == 'ontario_rea']
        ex_props = existing.get('properties', {})
        staged = {src: stage_read(src, pid) for src in ('gis', 'cwtd', 'osm')}
        groups = [rea]
        meta_sources = list(ex_props.get('sources') or []) if rea else []
        confs = [ex_props.get('confidence', 'high')] if rea else []
        notes = [ex_props['notes']] if rea and ex_props.get('notes') else []
        licenses = [ex_props['license']] if rea and ex_props.get('license') else []
        for src in ('gis', 'cwtd', 'osm'):
            fc = staged.get(src)
            if not fc or not fc.get('features'):
                continue
            groups.append(fc['features'])
            meta_sources += fc.get('properties', {}).get('sources') or []
            confs.append(fc.get('properties', {}).get('confidence', 'high'))
            notes += fc.get('properties', {}).get('notes') or []
            if fc.get('properties', {}).get('license'):
                licenses.append(fc['properties']['license'])
        feats = merge_groups(groups)
        if not feats:
            if pid in index:
                del index[pid]
                fp = os.path.join(FOOTPRINT_DIR, f'{pid}.json')
                if os.path.exists(fp):
                    os.remove(fp)
            continue
        p = by_pid.get(pid) or {'name': ex_props.get('name'), 'jurisdiction': ex_props.get('jurisdiction')}
        conf = sorted(confs, key=lambda c: CONF_RANK.get(c, 1))[-1] if confs else 'medium'
        entry = write_footprint(pid, feats, {
            'name': p.get('name'), 'jurisdiction': p.get('jurisdiction'),
            'source_label': '; '.join(sorted({SOURCE_LABEL.get(s.get('source') or 'ontario_rea', s.get('source'))
                                              for s in meta_sources} | ({'Ontario REA equipment schedule'} if rea else set()))),
            'sources': meta_sources, 'confidence': conf,
            'turbines_expected': ex_props.get('turbines_expected'),
            'notes': '; '.join(dict.fromkeys(n for n in notes if n)) or None,
            'license': '; '.join(dict.fromkeys(licenses)) or None,
        })
        if entry:
            index[pid] = entry
            n_written += 1
    save_index(index)
    roles = Counter()
    for e in index.values():
        roles.update(e.get('roles', {}))
    log(f'merge: {n_written} footprints written ({len(index)} in index); elements by role: {dict(roles)}')


def merge_groups(groups):
    """Earlier groups win. A source's own rows are always distinct units (an
    approval can list two inverters 3 m apart); across sources a point of the
    same role within `tol` metres is the same element -- turbines within 60 m
    (REA approved location vs CWTD as-built) -> keep the first, copy attrs."""
    out, prev_pts = [], []
    tol = {'turbine': 60.0}
    for grp in groups:
        own = []
        for f in grp:
            g = f.get('geometry') or {}
            if g.get('type') == 'Point':
                role = f['properties'].get('role')
                lon, lat = g['coordinates']
                dup = None
                for (r, la, lo, ref) in prev_pts:
                    if r == role and haversine_km(lat, lon, la, lo) * 1000 <= tol.get(role, 8.0):
                        dup = ref
                        break
                if dup is not None:
                    for k, v in f['properties'].items():
                        if k not in dup['properties'] and k not in ('source', 'source_doc', 'match', 'label', 'also_in'):
                            dup['properties'][k] = v
                    src = f['properties'].get('source')
                    also = dup['properties'].setdefault('also_in', [])
                    if src and src not in also and src != dup['properties'].get('source'):
                        also.append(src)
                    continue
                own.append((role, lat, lon, f))
            out.append(f)
        prev_pts += own
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', default='all', choices=['all', 'gis', 'cwtd', 'osm'])
    ap.add_argument('--budget', type=int, default=3600, help='seconds for the network phases')
    ap.add_argument('--limit', type=int, default=100000, help='max items per network source')
    ap.add_argument('--merge-only', action='store_true')
    ap.add_argument('--cwtd-file', help='local CWTD CSV/GeoJSON (offline test)')
    ap.add_argument('--osm-fixture', help='local Overpass JSON used for every project (offline test)')
    a = ap.parse_args()
    projects = load_projects()
    log(f'{len(projects)} registry projects')
    if not a.merge_only:
        budget = Budget(a.budget)
        if a.source in ('all', 'gis'):
            run_gis(projects, budget, a.limit)
        if a.source in ('all', 'cwtd'):
            run_cwtd(projects, a.cwtd_file)
        if a.source in ('all', 'osm'):
            fixture = json.load(open(a.osm_fixture)) if a.osm_fixture else None
            run_osm(projects, budget, a.limit, fixture)
    merge_all(projects)


if __name__ == '__main__':
    main()
