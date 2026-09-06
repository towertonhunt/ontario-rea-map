#!/usr/bin/env python3
"""Shared helpers for project footprints / layouts (data/footprints/).

A *footprint* is a small GeoJSON FeatureCollection per project holding the
design elements we could recover for it -- turbine / inverter / substation
points, pit / tailings / reservoir polygons, transmission / collector lines --
each feature tagged with a `role` (see ROLES) and its provenance (`source`,
`source_doc`). The map (index.html) lazy-loads them when a viewer zooms in.

File layout
  data/footprints/<pid>.json      one FeatureCollection per project (pid =
                                  make_id(jurisdiction, name), same stable id
                                  the API uses)
  data/footprints/index.json      {pid: {kind, n, bbox, roles: {role: n},
                                  sources: [..], ...}} -- what
                                  build_national_geojson attaches to features

Pure standard library on purpose: the extractors run both in Actions lanes
and on the dev Mac, and GDAL is never a given.
"""
import hashlib
import json
import math
import os
import re
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOOTPRINT_DIR = os.path.join(ROOT, 'data', 'footprints')
INDEX_PATH = os.path.join(FOOTPRINT_DIR, 'index.json')

# Must stay identical to scripts/build_api.py (the API and the footprints
# key projects the same way).
JUR_PREFIX = {
    'Federal (IAAC)': 'fed', 'British Columbia (EAO)': 'bc',
    'Quebec (MELCCFP)': 'qc', 'Nova Scotia (NSECC)': 'ns',
    'Newfoundland & Labrador (ECC)': 'nl', 'Manitoba (Environment Act)': 'mb',
    'Ontario (REA)': 'rea', 'Ontario (Provincial EA)': 'onp',
    'Ontario (Abandoned Mines)': 'amis',
    'Major projects inventory (unmatched)': 'gap',
}


def make_id(jur, name):
    h = hashlib.md5(f'{jur}||{name}'.encode()).hexdigest()[:10]
    return f'{JUR_PREFIX.get(jur, "x")}-{h}'


def fold(s):
    s = unicodedata.normalize('NFKD', str(s or ''))
    return s.encode('ascii', 'ignore').decode('ascii').lower().strip()


# Design-element roles. Keep this list in sync with FOOTPRINT_STYLES in
# index.html (unknown roles fall back to 'other' styling there).
ROLES = {
    # points
    'turbine': 'Wind turbine',
    'inverter': 'Inverter / power conversion station',
    'transformer': 'Transformer',
    'substation': 'Substation / switchyard',
    'met_tower': 'Meteorological tower',
    'generator': 'Generating unit',
    'shaft': 'Mine shaft / portal',
    'well': 'Well / wellpad',
    'dam': 'Dam structure',
    'powerhouse': 'Powerhouse',
    'tracker': 'Solar tracker drive',
    'crossing': 'Watercourse crossing / culvert',
    # lines
    'transmission_line': 'Transmission line',
    'collector_line': 'Collector / distribution line',
    'pipeline': 'Pipeline',
    'access_road': 'Access road',
    'rail': 'Rail spur',
    'penstock': 'Penstock / canal',
    # polygons
    'footprint': 'Project footprint / site boundary',
    'solar_array': 'Solar array',
    'pit': 'Open pit / quarry',
    'tailings': 'Tailings storage',
    'waste_rock': 'Waste rock / stockpile',
    'plant': 'Plant site / processing',
    'reservoir': 'Reservoir / headpond',
    'landfill': 'Landfill / disposal cell',
    'building': 'Building',
    'other': 'Other element',
}

# ── geodesy (pure python) ────────────────────────────────────────────
# GRS80 ellipsoid (NAD83) -- WGS84 differs by well under a metre, which
# is far below the precision of anything we extract.
_A = 6378137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_K0 = 0.9996


def utm_to_latlon(easting, northing, zone, northern=True):
    """UTM (m) -> (lat, lon) in degrees. Standard series expansion."""
    x = easting - 500000.0
    y = northing if northern else northing - 10000000.0
    e = math.sqrt(_E2)
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    ep2 = _E2 / (1 - _E2)
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    n1 = _A / math.sqrt(1 - _E2 * sin1 ** 2)
    t1 = tan1 ** 2
    c1 = ep2 * cos1 ** 2
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1 ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * tan1 / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / cos1
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    return math.degrees(lat), math.degrees(lon0 + lon)


def utm_zone_for_lon(lon):
    return int((lon + 180) // 6) + 1


def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def in_canada(lat, lon):
    return 41.5 < lat < 84 and -141.5 < lon < -52


# ── geometry helpers ─────────────────────────────────────────────────

def iter_coords(geom):
    t = geom.get('type')
    c = geom.get('coordinates')
    if t == 'Point':
        yield c
    elif t in ('MultiPoint', 'LineString'):
        yield from c
    elif t in ('MultiLineString', 'Polygon'):
        for ring in c:
            yield from ring
    elif t == 'MultiPolygon':
        for poly in c:
            for ring in poly:
                yield from ring
    elif t == 'GeometryCollection':
        for g in geom.get('geometries', []):
            yield from iter_coords(g)


def bbox_of(features):
    xs, ys = [], []
    for f in features:
        if not f.get('geometry'):
            continue
        for x, y in iter_coords(f['geometry']):
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return [round(min(xs), 6), round(min(ys), 6), round(max(xs), 6), round(max(ys), 6)]


def centroid_of(features):
    b = bbox_of(features)
    if not b:
        return None
    return ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2)  # lat, lon


def _perp_dist(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def simplify_ring(coords, tol):
    """Douglas-Peucker (iterative). tol in coordinate units (degrees)."""
    if len(coords) <= 4 or tol <= 0:
        return coords
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        dmax, idx = 0.0, None
        for k in range(i + 1, j):
            d = _perp_dist(coords[k], coords[i], coords[j])
            if d > dmax:
                dmax, idx = d, k
        if idx is not None and dmax > tol:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    out = [c for c, k in zip(coords, keep) if k]
    if len(out) < 4 and coords[0] == coords[-1]:
        return coords
    return out


def simplify_geometry(geom, tol=0.00002):
    """~2 m at Canadian latitudes -- keeps files small, invisible at z19."""
    t = geom.get('type')
    if t == 'LineString':
        return {'type': t, 'coordinates': simplify_ring(geom['coordinates'], tol)}
    if t == 'MultiLineString':
        return {'type': t, 'coordinates': [simplify_ring(r, tol) for r in geom['coordinates']]}
    if t == 'Polygon':
        return {'type': t, 'coordinates': [simplify_ring(r, tol) for r in geom['coordinates']]}
    if t == 'MultiPolygon':
        return {'type': t, 'coordinates': [[simplify_ring(r, tol) for r in poly]
                                           for poly in geom['coordinates']]}
    return geom


def round_geometry(geom, nd=6):
    def rnd(c):
        if isinstance(c[0], (int, float)):
            return [round(c[0], nd), round(c[1], nd)]
        return [rnd(x) for x in c]
    if geom.get('type') == 'GeometryCollection':
        return {'type': 'GeometryCollection',
                'geometries': [round_geometry(g, nd) for g in geom['geometries']]}
    return {'type': geom['type'], 'coordinates': rnd(geom['coordinates'])}


def geometry_kind(features):
    kinds = {f['geometry']['type'] for f in features if f.get('geometry')}
    has_poly = bool(kinds & {'Polygon', 'MultiPolygon'})
    has_line = bool(kinds & {'LineString', 'MultiLineString'})
    has_pt = bool(kinds & {'Point', 'MultiPoint'})
    if has_poly and (has_line or has_pt):
        return 'layout'
    if has_poly:
        return 'footprint'
    if has_line:
        return 'route'
    if has_pt:
        return 'elements'
    return 'none'


# ── role classification from free text ───────────────────────────────
_ROLE_RULES = [
    ('turbine', re.compile(r'\b(wtg|wind turbine|turbine|nordex|vestas|siemens|enercon|senvion|repower|acciona|gamesa|goldwind|\bge\b.*(mw|lnte|xle))', re.I)),
    ('met_tower', re.compile(r'\bmet(eorological)?\s*(tower|mast)\b|\bmet\b', re.I)),
    ('substation', re.compile(r'substation|switch(ing|yard| station)|transformer station|\bts\b|\bsub\b', re.I)),
    ('inverter', re.compile(r'inverter|power conversion|\binv\b|pcs\b|pcu\b', re.I)),
    ('transformer', re.compile(r'transformer|\btrans\b|\btrs?\b|\bxfmr\b|mva\b|kva\b', re.I)),
    ('generator', re.compile(r'generator|engine|\bgen\b|flare|digester|boiler|chp', re.I)),
]


def role_from_text(text):
    for role, rx in _ROLE_RULES:
        if rx.search(text or ''):
            return role
    return 'other'


# ── persistence ──────────────────────────────────────────────────────

def load_index():
    if os.path.exists(INDEX_PATH):
        return json.load(open(INDEX_PATH))
    return {}


def save_index(idx):
    os.makedirs(FOOTPRINT_DIR, exist_ok=True)
    json.dump(dict(sorted(idx.items())), open(INDEX_PATH, 'w'), ensure_ascii=False,
              separators=(',', ':'))


def footprint_path(pid):
    return os.path.join(FOOTPRINT_DIR, f'{pid}.json')


def load_footprint(pid):
    p = footprint_path(pid)
    if os.path.exists(p):
        return json.load(open(p))
    return None


def summarize(features):
    roles = {}
    for f in features:
        r = f['properties'].get('role', 'other')
        roles[r] = roles.get(r, 0) + 1
    return roles


def write_footprint(pid, features, meta):
    """Write data/footprints/<pid>.json and return the index entry.

    meta: {name, jurisdiction, sources: [...], notes: str|None, ...} --
    anything the map should show as provenance.
    """
    if not features:
        return None
    os.makedirs(FOOTPRINT_DIR, exist_ok=True)
    feats = []
    for f in features:
        g = round_geometry(simplify_geometry(f['geometry']))
        feats.append({'type': 'Feature', 'geometry': g, 'properties': f.get('properties', {})})
    fc = {'type': 'FeatureCollection', 'features': feats,
          'properties': {'pid': pid, **{k: v for k, v in meta.items() if v not in (None, [], {})}}}
    json.dump(fc, open(footprint_path(pid), 'w'), ensure_ascii=False, separators=(',', ':'))
    xs, ys = [], []
    for f in feats:
        for x, y in iter_coords(f['geometry']):
            xs.append(x)
            ys.append(y)
    entry = {
        'kind': geometry_kind(feats),
        'n': len(feats),
        'bbox': bbox_of(feats),
        'centroid': [round(sum(xs) / len(xs), 6), round(sum(ys) / len(ys), 6)] if xs else None,
        'roles': summarize(feats),
        'sources': sorted({f['properties'].get('source') for f in feats if f['properties'].get('source')}),
    }
    for k in ('name', 'jurisdiction', 'confidence', 'turbines_expected', 'notes'):
        if meta.get(k) not in (None, ''):
            entry[k] = meta[k]
    return entry


def merge_features(*groups, dedupe_m=8.0):
    """Merge feature lists from several sources; later groups fill gaps in
    earlier ones (earlier = more authoritative). Point features within
    `dedupe_m` metres with the same role are treated as the same element."""
    out = []
    pts = []  # (role, lat, lon)
    for group in groups:
        for f in group or []:
            g = f.get('geometry') or {}
            if g.get('type') == 'Point':
                lon, lat = g['coordinates']
                role = f['properties'].get('role')
                dup = False
                for r, la, lo in pts:
                    if r == role and haversine_km(lat, lon, la, lo) * 1000 <= dedupe_m:
                        dup = True
                        break
                if dup:
                    continue
                pts.append((role, lat, lon))
            out.append(f)
    return out
