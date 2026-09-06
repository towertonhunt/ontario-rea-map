#!/usr/bin/env python3
"""Seed the archive and the map with Class EA documents held locally.

Two collections that no registry ever received:

* Hydro One transmission Class EAs -- 55 projects, 319 PDFs, from the
  ontario-classea harvest (hydro_one_project_inventory.csv +
  hydro_one_document_inventory.csv map every file to its hydroone.com URL).
* The Detour Lake / West Detour EA record from the owner's project files:
  the 2010 Class EA ESRs, the 2011 CEAA comprehensive study set, the 2019
  West Detour ESR and its 2020 addendum, and the MNR Statement of
  Completion. Only the PDF EA deliverables are taken; working files
  (checklists, spreadsheets, internal notes, archaeological reviews) stay
  private -- see DETOUR_INCLUDE.

Outputs (all consumed by the normal build):
  data/raw/classea_projects.json           -> map features (source on_class_ea)
  data/docs/classea/<slug>.json            -> per-project catalogues
  data/raw/archive_manifest_classea.json.gz-> archive records (merged by readers)
  bucket keys: classea/<proponent>/<slug>/<file>

Run on the machine that holds the files with the R2_* variables exported:
  python3 scripts/seed_classea_local.py --dry-run
  python3 scripts/seed_classea_local.py
"""
import argparse
import csv
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_docs import MANIFEST, load_manifest, save_manifest, upload_dir  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H1 = os.path.join(os.path.dirname(ROOT), 'ontario-classea', 'transmission')
DETOUR = os.path.expanduser('~/Work/Detour Lake Mine/Detour Lake Mine Project/'
                            '01 Environmental Assessment')
OUT_PROJECTS = os.path.join(ROOT, 'data', 'raw', 'classea_projects.json')
OUT_DOCS = os.path.join(ROOT, 'data', 'docs', 'classea')
CLASSEA_MANIFEST = MANIFEST.replace('.json.gz', '_classea.json.gz')

# Detour: subfolders whose PDFs are public EA deliverables. Everything else in
# the folder is consulting work product and is deliberately not listed.
DETOUR_INCLUDE = {
    # subfolder -> (key slug [unchanged: bucket keys depend on it], section title)
    '2010 MNR Class EA': ('detour-lake-mine-mnr-class-ea', '2010 MNR Class EA (Detour Lake Project ESR)'),
    '2010 DLTPP Class EA': ('detour-lake-transmission-class-ea', '2010 Transmission Class EA (DLTPP Environmental Review Report)'),
    '2010 Detour Lake Power Project': ('detour-lake-power-project', '2010 Detour Lake Power Project EA (230 kV line)'),
    '2011 CEAA Comprehensive Study DLM': ('detour-lake-ceaa-comprehensive-study', '2011 CEAA Comprehensive Study'),
    '2019 WDP Environmental Study Report': ('west-detour-esr', '2019 West Detour Project ESR'),
    '2020 WDP Environmental Study Report Addendum': ('west-detour-esr', '2020 West Detour Project ESR Addendum'),
    'Basic Impact Assessment': ('west-detour-esr', '2020 West Detour Project ESR Addendum'),
}
# Never publish: review comments, responses, memos, notes, loose figures --
# anything that is not the original report. Moose Cree First Nation
# material in particular.
DETOUR_EXCLUDE = re.compile(r'Federal[_ ]Review|MCFN[_ ]Comments|Comments[_ ]on|Moose[_ ]Cree|Comment|Response|'
                            r'Consultation|SEA[_ ]review|\bMEM\b|\bNOTE\b|\bTAB\b|- Copy|^Figure', re.I)

DETOUR_ROOT_FILES = {  # loose files at the folder root that are public record
    'MNR_West Detour Project-Statement of Completion_March_23_2021.pdf':
        ('west-detour-esr', '2021 West Detour Statement of Completion (MNR)'),
}
DETOUR_COORDS = [-79.6878, 50.0228]      # Detour Lake mine, from the federal record


def safe(s):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', urllib.parse.unquote(s)).strip('_')[:140]


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:80]


def hydro_one():
    """-> (projects, docs) where docs = [(local_path, key, project_slug, title, source_url)]"""
    projects, docs = [], []
    inv = list(csv.DictReader(open(os.path.join(H1, 'hydro_one_project_inventory.csv'))))
    dinv = list(csv.DictReader(open(os.path.join(H1, 'hydro_one_document_inventory.csv'))))
    by_name = {}
    for d in dinv:
        by_name.setdefault(d['project_name'], []).append(d)
    local = {}
    for p in glob.glob(os.path.join(H1, 'documents', '*', '*.pdf')):
        local[os.path.basename(p).lower()] = p
    for r in inv:
        s = slug(r['project_name'])
        pdocs = []
        for d in by_name.get(r['project_name'], []):
            base = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(d['document_url']).path))
            path = local.get(base.lower())
            key = f'classea/hydro-one/{s}/{safe(base)}'
            pdocs.append({'title': d['document_title'] or base, 'url': d['document_url'],
                          'category': d.get('document_type') or 'other', 'key': key,
                          'local': path})
        projects.append({
            'slug': f'hydro-one-{s}', 'name': r['project_name'],
            'proponent': r.get('proponent') or 'Hydro One Networks Inc.',
            'jurisdiction': 'Ontario (Class EA)', 'source': 'on_class_ea',
            'class_ea': 'Class EA for Minor Transmission Facilities',
            'type': r.get('project_type') or 'Transmission',
            'status': r.get('status'), 'municipality': r.get('municipality'),
            'voltage_kv': r.get('voltage_kv'), 'length_km': r.get('length_km'),
            'registry_url': r.get('project_url'), 'proponent_url': r.get('project_url'),
            'description': r.get('description') or None, 'coords': None,
            'docs': pdocs,
        })
    return projects


def detour():
    docs = []
    for sub, (kslug, section) in DETOUR_INCLUDE.items():
        d = os.path.join(DETOUR, sub)
        if not os.path.isdir(d):
            continue
        for p in sorted(glob.glob(os.path.join(d, '**', '*.pdf'), recursive=True)):
            base = os.path.basename(p)
            if DETOUR_EXCLUDE.search(base.replace('_', ' ')):
                continue
            docs.append({'title': os.path.splitext(base)[0].replace('_', ' '), 'url': None,
                         'category': section,
                         'key': f'classea/detour-gold/{kslug}/{safe(sub)}/{safe(base)}', 'local': p})
    for base, (kslug, section) in DETOUR_ROOT_FILES.items():
        p = os.path.join(DETOUR, base)
        if os.path.exists(p):
            docs.append({'title': os.path.splitext(base)[0].replace('_', ' '), 'url': None,
                         'category': section,
                         'key': f'classea/detour-gold/{kslug}/{safe(base)}', 'local': p})
    order = {sec: i for i, (_, sec) in enumerate(list(DETOUR_INCLUDE.values())
                                                + list(DETOUR_ROOT_FILES.values()))}
    docs.sort(key=lambda d: (order.get(d['category'], 99), d['title']))
    return [{
        'slug': 'detour-gold-detour-lake-mine', 'name': 'Detour Lake Mine (incl. West Detour)',
        'proponent': 'Detour Gold Corporation (now Agnico Eagle Mines)',
        'jurisdiction': 'Ontario (Class EA)', 'source': 'on_class_ea',
        'class_ea': 'MNR Class EA (2010); Transmission Class EA (2010); Individual EA (2010); '
                    'CEAA 1992 Comprehensive Study (2011); MNDM Class EA ESR (2019-2021)',
        'type': 'Mining', 'status': 'Operating; West Detour Statement of Completion March 23, 2021',
        'municipality': 'Cochrane',
        'registry_url': 'https://iaac-aeic.gc.ca/archives/evaluations/52262/details-eng_pid=52262.html',
        'proponent_url': 'https://www.agnicoeagle.com/English/operations/operations/detour-lake/',
        'description': 'Open-pit gold mine 185 km northeast of Cochrane. The EA record spans the 2010 '
                       'provincial Class EAs and Individual EA, the 2011 federal comprehensive study '
                       '(CEAR 10-03-52262), and the West Detour expansion assessed as a provincial ESR '
                       'after the federal Minister declined to designate it in 2017.',
        'coords': DETOUR_COORDS,
        # the federal registry record for the same mine is folded into this point
        # registry records for the same mine are folded into this one point
        'merge_registry': [
            {'source': 'federal_iaac', 'project': '10-03-52262',
             'section': 'Federal registry (CEAR 10-03-52262)'},
            {'source': 'on_provincial_ea', 'project': 'detour-lake-power-project',
             'section': '2010 Detour Lake Power Project EA (ontario.ca record)'},
            {'source': 'on_provincial_ea', 'project': 'detour-lake-contingency-power-project',
             'section': '2012 Detour Lake Contingency Power Project EA (ontario.ca record)'},
            {'source': 'on_provincial_ea', 'project': 'detour-lake-mine-long-term-waste-management-strategy',
             'section': '2024 Long-term Waste Management Strategy EA (ontario.ca record)'},
        ],
        'docs': docs,
    }]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--verify', action='store_true',
                    help='HEAD every archived Class EA object first; forget records whose '
                         'object is gone so they are re-uploaded (after a manual bucket edit)')
    ap.add_argument('--no-upload', action='store_true',
                    help='write catalogues/projects for already-archived files only; '
                         'skip files that still need uploading (no credentials needed)')
    args = ap.parse_args()
    public = os.environ.get('R2_PUBLIC_BASE', '').rstrip('/')
    known = load_manifest()
    if not public:      # derive from any archived record (dry runs, catalogue-only runs)
        for v in known.values():
            if v.get('archive_url') and v.get('key') and v['archive_url'].endswith(v['key']):
                public = v['archive_url'][:-len(v['key'])].rstrip('/')
                break
    if not public:
        raise SystemExit('R2_PUBLIC_BASE is not set (export the R2_* variables first)')

    if args.verify:
        import urllib.request
        cm = json.load(gzip.open(CLASSEA_MANIFEST, 'rt')) if os.path.exists(CLASSEA_MANIFEST) else {}
        gone = []
        for u, rec in list(cm.items()):
            if not rec.get('archive_url'):
                continue
            try:
                req = urllib.request.Request(rec['archive_url'], method='HEAD')
                urllib.request.urlopen(req, timeout=30)
            except Exception as e:                                   # noqa: BLE001
                if 'HTTP Error 404' in str(e):
                    gone.append(u)
        for u in gone:
            del cm[u]
            known.pop(u, None)
        if gone:
            import gzip as _g
            json.dump(cm, _g.open(CLASSEA_MANIFEST, 'wt'), ensure_ascii=False)
        print(f'--verify: {len(gone)} archived objects no longer in the bucket; records forgotten')

    projects = hydro_one() + detour()
    all_docs = [(p, d) for p in projects for d in p['docs']]
    # a record is looked up by the URL the catalogue carries (source URL, else archive URL)
    to_upload = [(p, d) for p, d in all_docs
                 if d['local'] and not known.get(d['url'] or f'{public}/{d["key"]}', {}).get('key')]
    missing = [(p, d) for p, d in all_docs if not d['local']]
    n_bytes = sum(os.path.getsize(d['local']) for _, d in to_upload)
    print(f'{len(projects)} projects, {len(all_docs)} documents: {len(to_upload)} to upload '
          f'({n_bytes / 1e9:.2f} GB), {len(all_docs) - len(to_upload) - len(missing)} already '
          f'archived, {len(missing)} listed but not on disk')
    if args.dry_run:
        for p, d in to_upload[:6]:
            print('  ', d['key'])
        return

    if args.no_upload:
        skipped = {d['key'] for _, d in to_upload}
        to_upload = []
        print(f'--no-upload: {len(skipped)} unarchived files left out of the catalogues '
              f'until a credentialed run')
    else:
        skipped = set()
    staging = tempfile.mkdtemp(prefix='ea-classea-')
    seed = {}
    if os.path.exists(CLASSEA_MANIFEST):
        seed = json.load(gzip.open(CLASSEA_MANIFEST, 'rt'))
    for p, d in to_upload:
        dest = os.path.join(staging, d['key'])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(d['local'], dest)
        data = open(d['local'], 'rb').read()
        # keyed by the URL the catalogue will carry: the source URL where
        # there is one, else the archive URL itself -- so the lane, which
        # looks records up by catalogue url, never re-fetches these
        seed[d['url'] or f'{public}/{d["key"]}'] = {
            'jur': 'classea', 'project': p['slug'], 'title': d['title'][:200],
            'key': d['key'], 'archive_url': f'{public}/{d["key"]}', 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'content_type': 'application/pdf',
            'kind': 'file', 'source': 'local:classea',
            'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    print(f'uploading {len(to_upload)} files ({n_bytes / 1e9:.2f} GB)...', flush=True)
    if not upload_dir(staging):
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit('upload failed; nothing written')
    shutil.rmtree(staging, ignore_errors=True)
    save_manifest(seed, CLASSEA_MANIFEST)

    os.makedirs(OUT_DOCS, exist_ok=True)
    feats = []
    for p in projects:
        docs = []
        for d in p['docs']:
            if d['key'] in skipped:
                continue
            lookup = d['url'] or f'{public}/{d["key"]}'
            rec = seed.get(lookup) or known.get(lookup) or {}
            entry = {'title': d['title'], 'category': d['category'],
                     'url': d['url'] or rec.get('archive_url')}
            if rec.get('archive_url'):
                entry['archive_url'] = rec['archive_url']
            if entry['url']:
                docs.append(entry)
        json.dump({'project': p['name'], 'docs': docs},
                  open(os.path.join(OUT_DOCS, f"{p['slug']}.json"), 'w'), ensure_ascii=False)
        f = {k: v for k, v in p.items() if k != 'docs'}
        f['doc_count'] = len(docs)
        f['docs_path'] = f"data/docs/classea/{p['slug']}.json"
        feats.append(f)
    json.dump(feats, open(OUT_PROJECTS, 'w'), ensure_ascii=False, indent=1)
    print(f'done: {len(feats)} projects -> {OUT_PROJECTS}, catalogues in {OUT_DOCS}, '
          f'manifest {CLASSEA_MANIFEST}')


if __name__ == '__main__':
    main()
