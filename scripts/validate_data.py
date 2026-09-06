#!/usr/bin/env python3
"""Integrity checks over the project's data artifacts.

Fast, offline, re-runnable smoke test. Hard FAILs (schema/enum breaks that
would corrupt the map, predictor, or search) exit non-zero; soft WARNs
(suspicious-but-tolerable data, e.g. an out-of-Canada coordinate) are
reported but don't fail. Run after any data rebuild or before merging a
lane's output:  python3 scripts/validate_data.py
"""
import glob
import gzip
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DISCIPLINES = {
    'surface_water', 'groundwater', 'fish_fish_habitat', 'wetlands',
    'vegetation_ecosystems', 'wildlife_birds', 'species_at_risk',
    'air_quality', 'noise_vibration', 'light', 'soils_terrain',
    'waste_hazmat', 'accidents_malfunctions', 'human_health',
    'socio_economic', 'indigenous_rights_tluse', 'archaeology_heritage',
    'visual_landscape', 'climate_ghg', 'cumulative_effects',
    'closure_postclosure', 'other'}
MEASURE_TYPES = {
    'avoidance', 'minimization', 'mitigation', 'compensation_offset',
    'management_plan', 'monitoring_followup', 'financial_assurance',
    'engagement', 'other'}
TIMING = {'pre_construction', 'construction', 'operation', 'closure',
          'post_closure', 'all_phases', None}
# generous Canada bounding box (lon then lat)
CA_BBOX = (-141.5, 41.5, -52.0, 84.0)

fails, warns = [], []


def fail(msg):
    fails.append(msg)


def warn(msg):
    warns.append(msg)


def check_conditions():
    files = sorted(glob.glob(os.path.join(ROOT, 'data', 'conditions',
                                          '*_conditions_v2.json.gz')))
    if not files:
        return fail('no *_conditions_v2.json.gz found')
    total = 0
    for path in files:
        name = os.path.basename(path)
        recs = json.load(gzip.open(path, 'rt'))
        if not recs:
            fail(f'{name}: empty')
            continue
        # NOTE: condition_id is NOT unique by design — the federal source
        # repeats numbering across documents, so the merge dedupes on
        # (project, normalized text) and joins positionally. The real
        # invariant to verify is that text-level dedup held.
        seen_text = set()
        for i, r in enumerate(recs):
            where = f'{name}[{i}]'
            for k in ('discipline', 'measure_type', 'project',
                      'jurisdiction', 'measure_text'):
                if k not in r:
                    fail(f'{where}: missing key {k}')
            if r.get('discipline') not in DISCIPLINES:
                fail(f'{where}: bad discipline {r.get("discipline")!r}')
            if r.get('measure_type') not in MEASURE_TYPES:
                fail(f'{where}: bad measure_type {r.get("measure_type")!r}')
            if r.get('timing') not in TIMING:
                fail(f'{where}: bad timing {r.get("timing")!r}')
            secs = r.get('discipline_secondary') or []
            if not isinstance(secs, list) or any(s not in DISCIPLINES
                                                 for s in secs):
                fail(f'{where}: bad discipline_secondary {secs!r}')
            if r.get('discipline') in secs:
                fail(f'{where}: primary repeated in secondary')
            # verify the merge's text-level dedup held
            key = (r.get('project'),
                   re.sub(r'\W+', '', (r.get('measure_text') or '').lower())[:400])
            if key in seen_text:
                warn(f'{name}: dedup miss — repeated (project, text): '
                     f'{str(r.get("project"))[:40]!r} / {r.get("condition_id")}')
            seen_text.add(key)
        total += len(recs)
        print(f'  ok  {name}: {len(recs)} conditions')
    print(f'  -> {total} conditions across {len(files)} jurisdictions')


def check_geojson():
    path = os.path.join(ROOT, 'data', 'projects_canada.geojson')
    if not os.path.exists(path):
        return fail('projects_canada.geojson missing')
    d = json.load(open(path))
    feats = d.get('features')
    if d.get('type') != 'FeatureCollection' or not isinstance(feats, list):
        return fail('projects_canada.geojson: not a FeatureCollection')
    mapped = oob = 0
    for i, f in enumerate(feats):
        p = f.get('properties') or {}
        if not p.get('name'):
            fail(f'feature[{i}]: no name')
        if not p.get('jurisdiction'):
            fail(f'feature[{i}]: no jurisdiction')
        g = f.get('geometry')
        if g:
            c = g.get('coordinates')
            if (not isinstance(c, list) or len(c) != 2
                    or not all(isinstance(x, (int, float)) for x in c)):
                fail(f'feature[{i}] ({p.get("name","?")[:30]}): bad coords {c!r}')
                continue
            mapped += 1
            lon, lat = c
            if not (CA_BBOX[0] <= lon <= CA_BBOX[2]
                    and CA_BBOX[1] <= lat <= CA_BBOX[3]):
                oob += 1
                warn(f'{p.get("jurisdiction")}: "{p.get("name","?")[:40]}" '
                     f'at [{lon:.3f},{lat:.3f}] outside Canada bbox')
    print(f'  ok  geojson: {len(feats)} features, {mapped} mapped, '
          f'{oob} out-of-bounds')


# URL shapes we have shipped broken before. Each entry is (regex, why) and is
# a hard FAIL: these were live on the map and every one of them was dead.
BAD_URL_PATTERNS = [
    (re.compile(r'/050/evaluations/proj/[\w]+-'),
     'archived CEAR project id on the live registry path (404s); '
     'use https://iaac-aeic.gc.ca/archives/evaluations + relative_path'),
    (re.compile(r'ree\.environnement\.gouv\.qc\.ca/fiche\.asp'),
     'Quebec REE renamed fiche.asp -> projet.asp (all 402 links 404d)'),
]


def check_links():
    """Structural link checks (offline) + the last sweep's result if present."""
    path = os.path.join(ROOT, 'data', 'projects_canada.geojson')
    if not os.path.exists(path):
        return
    feats = json.load(open(path))['features']
    relative = bad = 0
    seen_bad = {}
    for f in feats:
        u = (f.get('properties') or {}).get('registry_url')
        if not u:
            continue
        if not u.startswith(('http://', 'https://')):
            relative += 1
            continue
        for rx, why in BAD_URL_PATTERNS:
            if rx.search(u):
                bad += 1
                seen_bad.setdefault(why, u)
    if relative:
        fail(f'{relative} features have a registry_url that is not an '
             f'absolute URL (renders as a dead relative link)')
    for why, example in seen_bad.items():
        fail(f'known-dead URL shape back in the data: {why} (e.g. {example})')
    if not relative and not bad:
        print(f'  ok  links: no known-dead URL shapes in '
              f'{sum(1 for f in feats if (f.get("properties") or {}).get("registry_url"))} '
              f'registry links')

    health = os.path.join(ROOT, 'data', 'link_health.json')
    if not os.path.exists(health):
        return warn('no data/link_health.json; run scripts/check_links.py')
    h = json.load(open(health))
    checked, broken = h.get('total', 0), h.get('broken', 0)
    if checked and broken / checked > 0.02:
        warn(f'last link sweep ({h.get("checked_utc")}): {broken}/{checked} '
             f'links broken (>2%)')
    else:
        print(f'  ok  last link sweep {h.get("checked_utc")}: '
              f'{broken}/{checked} broken')


def check_corpus_search():
    path = os.path.join(ROOT, 'data', 'corpus_search.sqlite3.gz')
    if not os.path.exists(path):
        return warn('corpus_search.sqlite3.gz missing (run '
                    'build_corpus_search.py)')
    try:
        raw = gzip.open(path, 'rb').read()
        tmp = os.path.join(ROOT, 'data', '.validate_tmp.sqlite3')
        open(tmp, 'wb').write(raw)
        db = sqlite3.connect(tmp)
        n = db.execute('SELECT count(*) FROM docs').fetchone()[0]
        hit = db.execute("SELECT count(*) FROM docs WHERE docs MATCH "
                         "'environment'").fetchone()[0]
        db.close()
        os.remove(tmp)
        if n < 100:
            fail(f'corpus search: only {n} docs indexed')
        else:
            print(f'  ok  corpus search: {n} docs, MATCH returns {hit} hits')
    except Exception as e:
        fail(f'corpus search DB unreadable: {e}')


def check_predictions():
    files = glob.glob(os.path.join(ROOT, 'data', 'predictions', '*.json'))
    if not files:
        return warn('no prediction registers (run mitigation_predict --full)')
    for path in files:
        d = json.load(open(path))
        for row in d.get('strong_precedents', []):
            if row['discipline'] not in DISCIPLINES:
                fail(f'{os.path.basename(path)}: bad discipline '
                     f'{row["discipline"]!r}')
    print(f'  ok  predictions: {len(files)} registers')


def check_gap_report():
    """Soft surface of gap reconciliation output. Not an invariant — the
    report is advisory — but a high-value likely-missing major is worth a
    daily nudge. Absent report is fine (lane may not have run yet)."""
    path = os.path.join(ROOT, 'data', 'gap_report.json')
    if not os.path.exists(path):
        return
    try:
        rep = json.load(open(path))
    except Exception as e:
        return warn(f'gap_report.json unreadable: {e}')
    gaps = [x for x in rep.get('results', []) if x.get('verdict') == 'gap']
    if not gaps:
        print('  ok  gap report: no unmatched majors')
        return
    majors = [x for x in gaps
              if isinstance(x['ext'].get('value_cad'), (int, float))
              and x['ext']['value_cad'] >= 1e9]
    print(f'  ok  gap report: {len(gaps)} unmatched, {len(majors)} >= $1B')
    for x in majors[:5]:
        warn(f'likely-missing major: {x["ext"].get("name")} '
             f'(${x["ext"]["value_cad"]/1e9:.1f}B, {x["ext"].get("province")})')


def check_known_projects():
    """Regression guard: named majors that must stay findable (see the
    checklist's _doc). Levels: present (map feature) > visible (feature or
    gap pin) > tracked (feature, pin, or weak/review row in gap_report).
    Below-expectation = WARN — the signal a major is hiding again."""
    path = os.path.join(ROOT, 'data', 'known_projects_checklist.json')
    if not os.path.exists(path):
        return
    import unicodedata

    def fold(s):
        s = unicodedata.normalize('NFKD', str(s or ''))
        return s.encode('ascii', 'ignore').decode('ascii').lower()

    checks = json.load(open(path))['checks']
    gj = json.load(open(os.path.join(ROOT, 'data', 'projects_canada.geojson')))
    feat_names, pin_names = [], []
    for f in gj['features']:
        p = f['properties']
        (pin_names if p.get('source') == 'nrcan_gap' else feat_names).append(
            fold(p.get('name')))
    review_names = []
    gr_path = os.path.join(ROOT, 'data', 'gap_report.json')
    if os.path.exists(gr_path):
        for x in json.load(open(gr_path)).get('results', []):
            if x.get('verdict') in ('weak', 'gap'):
                review_names.append(fold((x.get('ext') or {}).get('name')))

    LEVELS = {'present': 3, 'visible': 2, 'tracked': 1}
    n_ok = 0
    for c in checks:
        variants = [fold(v) for v in c['variants']]
        if any(v in n for v in variants for n in feat_names):
            got, got_lvl = 'present', 3
        elif any(v in n for v in variants for n in pin_names):
            got, got_lvl = 'gap-pinned', 2
        elif any(v in n for v in variants for n in review_names):
            got, got_lvl = 'review-row', 1
        else:
            got, got_lvl = 'ABSENT', 0
        if got_lvl >= LEVELS[c['expect']]:
            n_ok += 1
        else:
            warn(f'known project below expectation: {c["name"]} — '
                 f'{got} (expected >= {c["expect"]}) · {c["why"]}')
    print(f'  ok  known projects: {n_ok}/{len(checks)} at or above expectation')


def check_api():
    """Static API (data/api/) integrity: index parses, row count matches
    the geojson, ids unique, deep-tier detail files exist and parse."""
    idx_p = os.path.join(ROOT, 'data', 'api', 'projects.json')
    if not os.path.exists(idx_p):
        return warn('no data/api yet (run scripts/build_api.py)')
    idx = json.load(open(idx_p))
    gj = json.load(open(os.path.join(ROOT, 'data', 'projects_canada.geojson')))
    if len(idx) != len(gj['features']):
        fail(f'api index {len(idx)} rows != geojson {len(gj["features"])} '
             '(rerun build_api.py after build_national_geojson.py)')
    ids = [r['i'] for r in idx]
    if len(set(ids)) != len(ids):
        fail('api index ids not unique')
    meta = json.load(open(os.path.join(ROOT, 'data', 'api', 'meta.json')))
    missing = 0
    for pid in meta.get('deep_tier_ids', []):
        p = os.path.join(ROOT, 'data', 'api', 'project', f'{pid}.json')
        if not os.path.exists(p):
            missing += 1
        else:
            json.load(open(p))
    if missing:
        fail(f'{missing} deep-tier detail files missing')

    # Northey landmark cases: every crosswalk map id must be a real feature,
    # carry a detail file with a northey block, and set the index nth flag.
    id_set = set(ids)
    nth_ids = meta.get('northey_ids', [])
    nth_missing = 0
    for pid in nth_ids:
        if pid not in id_set:
            fail(f'northey id {pid} not in api index')
        p = os.path.join(ROOT, 'data', 'api', 'project', f'{pid}.json')
        if not os.path.exists(p) or 'northey' not in json.load(open(p)):
            nth_missing += 1
    if nth_missing:
        fail(f'{nth_missing} northey detail files missing or lack northey block')
    flagged = sum(1 for r in idx if r.get('nth'))
    if flagged != len(nth_ids):
        fail(f'index nth flags ({flagged}) != northey_ids ({len(nth_ids)})')

    print(f'  ok  api: {len(idx)} rows, {len(meta.get("deep_tier_ids", []))} '
          f'detail files, {len(nth_ids)} Northey landmark cases')


def check_footprints():
    """Project layouts (data/footprints/): index <-> files <-> geojson pointers."""
    fp_dir = os.path.join(ROOT, 'data', 'footprints')
    idx_path = os.path.join(fp_dir, 'index.json')
    if not os.path.exists(idx_path):
        print('  --  no footprints index yet (run scripts/extract_rea_layouts.py)')
        return
    idx = json.load(open(idx_path))
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    from footprints_common import ROLES, iter_coords
    n_el = bad = unknown_roles = 0
    for pid, e in idx.items():
        path = os.path.join(fp_dir, f'{pid}.json')
        if not os.path.exists(path):
            fail(f'footprints: index entry {pid} has no file'); bad += 1; continue
        try:
            fc = json.load(open(path))
        except Exception as ex:
            fail(f'footprints: {pid}.json unreadable: {ex}'); bad += 1; continue
        feats = fc.get('features') or []
        if fc.get('type') != 'FeatureCollection' or not feats:
            fail(f'footprints: {pid}.json is not a non-empty FeatureCollection'); bad += 1; continue
        if len(feats) != e.get('n'):
            fail(f'footprints: {pid} index n={e.get("n")} but file has {len(feats)}')
        for f in feats:
            g = f.get('geometry') or {}
            role = (f.get('properties') or {}).get('role')
            if role not in ROLES:
                unknown_roles += 1
            for lon, lat in iter_coords(g):
                if not (CA_BBOX[0] <= lon <= CA_BBOX[2] and CA_BBOX[1] <= lat <= CA_BBOX[3]):
                    fail(f'footprints: {pid} ({e.get("name", "?")[:30]}) element outside Canada '
                         f'[{lon:.3f},{lat:.3f}]')
                    break
        n_el += len(feats)
    if unknown_roles:
        warn(f'footprints: {unknown_roles} elements with a role not in ROLES')
    # every geojson pointer must resolve
    geo = os.path.join(ROOT, 'data', 'projects_canada.geojson')
    if os.path.exists(geo):
        missing = 0
        pointed = 0
        for f in json.load(open(geo))['features']:
            fp = (f.get('properties') or {}).get('footprint_path')
            if not fp:
                continue
            pointed += 1
            if not os.path.exists(os.path.join(ROOT, fp)):
                missing += 1
        if missing:
            fail(f'footprints: {missing} geojson footprint_path pointers have no file')
        if not pointed and idx:
            warn('footprints: index has entries but no geojson feature points at one '
                 '(rebuild with scripts/build_national_geojson.py)')
        print(f'  ok  footprints: {len(idx)} projects, {n_el} elements, '
              f'{pointed} geojson pointers, {bad} broken')


def check_catalogues():
    """Per-project document catalogues must list each file once and give
    same-titled files a distinguishing suffix (split_doc_catalogues.py
    normalises both; any writer that skips it shows up here)."""
    import collections
    import glob
    n_files = n_docs = dup_files = dup_docs = same_files = same_docs = 0
    for path in glob.glob(os.path.join(ROOT, 'data', 'docs', '*', '*.json')):
        try:
            docs = json.load(open(path)).get('docs') or []
        except (ValueError, OSError):
            fail(f'unreadable catalogue {os.path.relpath(path, ROOT)}')
            continue
        n_files += 1
        n_docs += len(docs)
        urls = collections.Counter((d.get('url') or '').strip() for d in docs)
        d = sum(v - 1 for v in urls.values() if v > 1)
        if d:
            dup_files += 1
            dup_docs += d
        titles = collections.Counter((' '.join((d.get('title') or '').split()).casefold(),
                                      d.get('date') or '') for d in docs)
        t = sum(v for v in titles.values() if v > 1)
        if t:
            same_files += 1
            same_docs += t
    print(f'  {n_docs} documents in {n_files} catalogues')
    if dup_docs:
        fail(f'{dup_docs} repeated document URLs in {dup_files} catalogues '
             f'(run scripts/split_doc_catalogues.py --normalize-only)')
    if same_docs:
        warn(f'{same_docs} same-titled documents in {same_files} catalogues '
             f'not told apart by date or suffix')


def main():
    print('Validating data artifacts...\n')
    for name, fn in (('conditions', check_conditions),
                     ('geojson', check_geojson),
                     ('links', check_links),
                     ('corpus search', check_corpus_search),
                     ('predictions', check_predictions),
                     ('gap report', check_gap_report),
                     ('known projects', check_known_projects),
                     ('api', check_api),
                     ('catalogues', check_catalogues),
                     ('footprints', check_footprints)):
        print(f'[{name}]')
        try:
            fn()
        except Exception as e:
            fail(f'{name} check crashed: {e}')
        print()

    if warns:
        print(f'{len(warns)} WARNING(S):')
        for w in warns:
            print(f'  ! {w}')
        print()
    if fails:
        print(f'{len(fails)} FAILURE(S):')
        for f in fails:
            print(f'  X {f}')
        sys.exit(1)
    print('All checks passed.')


if __name__ == '__main__':
    main()
