#!/usr/bin/env python3
"""Extract project layouts from Ontario Renewable Energy Approvals (offline).

Every REA instrument carries a schedule ("Coordinates of the Equipment and
Noise Specifications", Table B1 ...) listing each turbine / inverter /
transformer / substation with its UTM easting + northing, sound power level
and equipment description -- the as-approved design, filed by the ministry.
The corpus already holds the text (data/corpus/ontario/rea-*.txt.gz), so
this needs no network.

For each corpus doc:
  1. match the project by name to the ontario_rea map feature
  2. find coordinate rows: <id> [dBA] <easting> <northing> [description]
     (integers or decimals, with or without thousands separators; some
     tables list Northing before Easting -- detected from the header)
  3. UTM zone: parsed from the text ("Z17-NAD83", "Zone 17") when present,
     otherwise the zone (15-18) that lands the points nearest the project
     pin. Points farther than MAX_KM from the pin are dropped (page-number
     / table-total false positives).
  4. role from the id + description; dedupe identical elements.
Writes data/footprints/rea-<hash>.json and merges into index.json.

Run:  python3 scripts/extract_rea_layouts.py [--verbose]
"""
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from footprints_common import (ROOT, haversine_km, in_canada, load_index, make_id,  # noqa: E402
                               role_from_text, save_index, utm_to_latlon, write_footprint)

CORPUS = os.path.join(ROOT, 'data', 'corpus', 'ontario')
GEOJSON = os.path.join(ROOT, 'data', 'projects_canada.geojson')
MAX_KM = 35.0          # element must sit within this of the project pin
MIN_ROWS = 1

# easting 6 digits (optionally 3,3 / 3 3), northing 7 digits, optional decimals
NUM_E = r'(?P<e>[2-7]\d{2}[, ]?\d{3}(?:\.\d+)?)'
NUM_N = r'(?P<n>[4-6][, ]?\d{3}[, ]?\d{3}(?:\.\d+)?)'
GAP = r'(?:[ \t]*\n?[ \t]*\n?[ \t]*)'          # same line, or a wrapped/blank line
PAIR_EN = re.compile(r'(?<![\d.,])' + NUM_E + GAP + NUM_N + r'(?![\d.,])')
PAIR_NE = re.compile(r'(?<![\d.,])' + NUM_N + GAP + NUM_E + r'(?![\d.,])')
ZONE_RX = re.compile(r'(?:UTM\s*[-, ]?\s*(?:Z(?:one)?)?\s*[-:]?\s*|\bZ(?:one)?\s*[-:]?\s*)(1[5-8])(?:\s*N)?\b', re.I)
# equipment ids: T1, WTG-1, INV1, I-1A, Tr301, BC-01, G_INV1, D_INV 1, S-1, #3
ID_STRONG = re.compile(r'\b([A-Za-z]{1,8}[-_ ]?\d{1,3}[A-Za-z]?)\b')
ID_WORD = re.compile(r'\b(sub(?:station)?|switch(?:ing)?\s*station|transformer(?:\s*(?:station|substation|#?\s*\d+))?|'
                     r'inverter(?:\s*#?\s*\d+)?|met(?:\s*tower)?|wtg|turbine(?:\s*#?\s*\d+)?|reactor|flare|'
                     r'generator(?:\s*#?\s*\d+)?|engine(?:\s*#?\s*\d+)?)\b', re.I)
LEVEL_RX = re.compile(r'(?<![\d.,-])(\d{2,3}(?:\.\d)?)\*{0,2}(?![\d.,])')
STOP = re.compile(r'^(see|details?|below|above|table|and|the|of|centre|center|m|\(m\)|dba|\(dba\))$', re.I)

# glue numbers the PDF text layer wrapped across lines:
#   "410519.\n1" -> 410519.1   "5002216\n.3" -> 5002216.3   "4,902,06\n7" -> 4,902,067
_GLUE = [
    (re.compile(r'(\d[\d,]*\.)[ \t]*\n[ \t]*(\d{1,2})(?!\d)'), r'\1\2'),
    (re.compile(r'(\d[\d,]*\d)[ \t]*\n[ \t]*(\.\d{1,2})(?!\d)'), r'\1\2'),
    (re.compile(r'(\d{1,3}(?:,\d{3})*,\d{1,2})[ \t]*\n[ \t]*(\d)(?!\d)'), r'\1\2'),
    (re.compile(r'(\d{1,3}(?:,\d{3})*,\d{1})[ \t]*\n[ \t]*(\d{2})(?!\d)'), r'\1\2'),
]


def num(s):
    return float(s.replace(',', '').replace(' ', ''))


def glue(text):
    for rx, rep in _GLUE:
        text = rx.sub(rep, text)
    return text


def load_rea_features():
    d = json.load(open(GEOJSON))
    out = {}
    for f in d['features']:
        p = f['properties']
        if p.get('source') != 'ontario_rea':
            continue
        out[p['name'].strip().lower()] = f
    return out


def _lines_before(text, pos, n=3):
    """Last n non-empty lines ending at pos (the line containing pos first)."""
    head = text[max(0, pos - 400):pos]
    parts = [ln.strip() for ln in head.split('\n')]
    cur = parts[-1]
    prev = [p for p in parts[:-1] if p][-n:]
    return cur, prev


def _lines_after(text, pos, n=2):
    tail = text[pos:pos + 300]
    parts = tail.split('\n')
    cur = parts[0].strip()
    nxt = [p.strip() for p in parts[1:] if p.strip()][:n]
    return cur, nxt


ID_TIGHT = re.compile(r'(?<![A-Za-z0-9])([A-Za-z][A-Za-z_]{0,9}[-_]?#?\d{1,3}[A-Za-z]?)(?![A-Za-z0-9])')
ID_SPACED = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{1,6} #?\d{1,3}[A-Za-z]?)(?![A-Za-z0-9])')
ID_JUNK = re.compile(r'^(table|fig|figure|schedule|section|page|npc|ge|mw|kv|mva|kva|db|dba|hz|no|see|b|a)\b', re.I)


def _id_in(ch):
    for rx in (ID_TIGHT, ID_SPACED):
        for m in rx.finditer(ch):
            t = m.group(1)
            if ID_JUNK.match(t) or LEVEL_RX.fullmatch(t):
                continue
            return t
    return None


def _pick_ident(chunks):
    """chunks: candidate strings nearest-first. Return (ident, chunk_used)."""
    for ch in chunks:
        t = _id_in(ch)
        if t:
            return t, ch
        m = ID_WORD.search(ch)
        if m:
            return m.group(0), ch
    for ch in chunks:
        m = re.match(r'^\s*#?(\d{1,3})\b', ch)
        if m:
            return m.group(1), ch
    return None, None


def parse_rows(text):
    """Yield (id, level, easting, northing, description, pos)."""
    text = glue(text)
    found = []
    for rx in (PAIR_EN, PAIR_NE):
        for m in rx.finditer(text):
            try:
                e, n = num(m.group('e')), num(m.group('n'))
            except ValueError:
                continue
            if 200000 <= e <= 800000 and 4500000 <= n <= 6400000:
                found.append((m.start(), m.end(), e, n))
    found.sort()
    rows, last_end = [], -1
    for start, end, e, n in found:
        if start < last_end:
            continue
        last_end = end
        cur, prev = _lines_before(text, start)
        after, nxt = _lines_after(text, end)
        # the row's own text is the current line (before the numbers); when it
        # holds no id, the label sits on the line(s) above (wrapped table)
        chunks = [cur] + prev[::-1]
        ident, used = _pick_ident(chunks)
        ctx = [cur]
        if used is not None and used is not cur:
            ctx = prev[prev.index(used):] + [cur] if used in prev else [used, cur]
        pre_text = ' '.join(ctx)
        level = None
        body = pre_text
        if ident:
            i = body.find(ident)
            if i >= 0:
                body = body[:i] + ' ' + body[i + len(ident):]
        for lm in LEVEL_RX.finditer(body):
            v = float(lm.group(1))
            if 40 <= v <= 130:
                level = v
        if level is not None:
            body = LEVEL_RX.sub(lambda mm: '' if 40 <= float(mm.group(1)) <= 130 else mm.group(0), body, count=1)
        desc_parts = [after] if after else []
        if not after and nxt:
            desc_parts = nxt[:1]
        elif after and nxt and len(after) < 25 and not re.search(r'\d{3}', nxt[0]):
            desc_parts.append(nxt[0])
        body = ' '.join(w for w in body.split() if not STOP.match(w))
        desc = ' '.join(desc_parts + ([body] if body and body != ident else []))
        desc = re.sub(r'\s+', ' ', desc).strip(' ,;:-')[:90]
        rows.append((ident or '', level, e, n, desc, start))
    return rows


def classify(ident, desc):
    role = role_from_text(f'{ident} {desc}'.replace('_', ' '))
    if role == 'other':
        if re.match(r'^(t|wtg|wt|turbine)\W*\d+', ident, re.I):
            role = 'turbine'
        elif re.match(r'^(inv|pcs|pcu|i)\W*\d+', ident, re.I):
            role = 'inverter'
        elif re.match(r'^(tr|trs|xf|trans|t)\W*\d*$', ident, re.I) and 'tr' in ident.lower():
            role = 'transformer'
        elif re.match(r'^(ss|sub|ts|s)\W*\d*$', ident, re.I):
            role = 'substation'
        elif re.search(r'medium voltage|mv station|power (?:conversion|station)|pcs', desc, re.I) or re.match(r'^mv\W*\d+', ident, re.I):
            role = 'inverter'
        elif re.match(r'^tran\W*\d+', ident, re.I) or re.search(r'\btran\b', desc, re.I):
            role = 'transformer'
        elif re.search(r'tracking|tracker', desc, re.I):
            role = 'tracker'
        elif re.search(r'culvert|crossing|watercourse', desc, re.I):
            role = 'crossing'
    return role


def choose_zone(rows, text, pin):
    """UTM zone for a document's rows. A zone stated in the text wins unless
    it lands the rows far from the pin while another zone lands them close
    (a mis-parsed zone); with no stated zone, the zone nearest the pin."""
    m = ZONE_RX.search(text)
    stated = int(m.group(1)) if m else None
    plat, plon = pin

    def median_dist(zone):
        d = sorted(haversine_km(*utm_to_latlon(e, n, zone), plat, plon) for _, _, e, n, _, _ in rows)
        return d[len(d) // 2]

    dists = {z: median_dist(z) for z in (15, 16, 17, 18)}
    nearest = min(dists, key=dists.get)
    if stated:
        if dists[stated] <= MAX_KM or dists[nearest] > MAX_KM:
            return stated, 'stated'
        return nearest, 'inferred'
    return nearest, 'inferred'


def main():
    verbose = '--verbose' in sys.argv
    idx_docs = json.load(open(os.path.join(CORPUS, 'index.json')))
    feats = load_rea_features()
    by_pid = defaultdict(list)      # pid -> [(doc, rows, text)]
    stats = Counter()
    for doc in idx_docs:
        key = (doc.get('project') or '').strip().lower()
        f = feats.get(key)
        if not f:
            stats['unmatched_doc'] += 1
            continue
        path = os.path.join(CORPUS, f"{doc['doc_id']}.txt.gz")
        if not os.path.exists(path):
            stats['missing_text'] += 1
            continue
        text = gzip.open(path, 'rt', errors='replace').read()
        rows = parse_rows(text)
        if len(rows) < MIN_ROWS:
            stats['no_rows'] += 1
            continue
        by_pid[make_id(f['properties']['jurisdiction'], f['properties']['name'])].append((doc, rows, text, f))
        stats['docs_with_rows'] += 1

    index = load_index()
    written = 0
    for pid, items in by_pid.items():
        f = items[0][3]
        p = f['properties']
        if not f.get('geometry'):
            stats['no_pin'] += 1
            continue
        plon, plat = f['geometry']['coordinates']
        features, seen, sources, zones = [], set(), [], set()
        dropped_far = 0
        for doc, rows, text, _ in items:
            zone, how = choose_zone(rows, text, (plat, plon))
            zones.add(zone)
            pts = [utm_to_latlon(e, n, zone) for _, _, e, n, _, _ in rows]
            # anchor: the pin when the zone had to be inferred from it; when
            # the document states the zone, the rows' own median -- a wrong
            # registry pin (Grey Highlands sits 400 km off) must not veto them
            if how == 'stated' and pts:
                lats = sorted(x[0] for x in pts)
                lons = sorted(x[1] for x in pts)
                alat, alon = lats[len(lats) // 2], lons[len(lons) // 2]
            else:
                alat, alon = plat, plon
            for (ident, level, e, n, desc, line_no), (lat, lon) in zip(rows, pts):
                if not in_canada(lat, lon) or haversine_km(lat, lon, alat, alon) > MAX_KM:
                    dropped_far += 1
                    continue
                role = classify(ident, desc)
                k = (role, round(e), round(n))
                if k in seen:
                    continue
                seen.add(k)
                props = {'role': role, 'label': ident or None, 'source': 'ontario_rea',
                         'source_doc': doc['doc_id'], 'utm_zone': zone}
                if desc:
                    props['description'] = desc
                if level:
                    props['sound_power_dba'] = level
                features.append({'type': 'Feature',
                                 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                                 'properties': props})
            sources.append({'doc_id': doc['doc_id'], 'title': doc.get('title'), 'url': doc.get('url'),
                            'rows': len(rows), 'utm_zone': zone, 'zone_how': how})
        if not features:
            stats['all_dropped'] += 1
            continue
        n_turb = sum(1 for x in features if x['properties']['role'] == 'turbine')
        exp = p.get('num_turbines')
        conf = 'high'
        notes = None
        if exp and n_turb:
            if n_turb < exp * 0.8:
                conf, notes = 'medium', f'{n_turb} turbine coordinates found; approval lists {exp}'
            elif n_turb > exp:
                notes = f'{n_turb} approved turbine locations; the approval permits up to {exp} to be built (alternates included)'
                if n_turb > exp * 1.3:
                    conf = 'medium'
        if dropped_far:
            notes = (notes + '; ' if notes else '') + f'{dropped_far} rows dropped (>{MAX_KM:.0f} km from the layout)'
        clat = sum(x['geometry']['coordinates'][1] for x in features) / len(features)
        clon = sum(x['geometry']['coordinates'][0] for x in features) / len(features)
        pin_off = haversine_km(clat, clon, plat, plon)
        if pin_off > 20:
            notes = (notes + '; ' if notes else '') + f'registry pin was {pin_off:.0f} km from the approved equipment; map pin moved to the layout'
        entry = write_footprint(pid, features, {
            'name': p['name'], 'jurisdiction': p['jurisdiction'],
            'source_label': 'Ontario Renewable Energy Approval (equipment coordinates schedule)',
            'sources': sources, 'confidence': conf, 'turbines_expected': exp, 'notes': notes,
            'license': 'Open Government Licence – Ontario',
        })
        if entry:
            entry['pin_offset_km'] = round(pin_off, 1)
            index[pid] = entry
            written += 1
            if verbose:
                print(f"{pid} {p['name'][:50]:50} {entry['roles']} conf={conf} zones={sorted(zones)}")
    save_index(index)
    print(f'REA layouts: {written} projects written; stats={dict(stats)}')
    roles = Counter()
    for pid in by_pid:
        for r, n in index.get(pid, {}).get('roles', {}).items():
            roles[r] += n
    print('elements:', dict(roles))


if __name__ == '__main__':
    main()
