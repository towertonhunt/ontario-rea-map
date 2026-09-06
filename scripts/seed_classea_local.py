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
    '2010 Detour Lake Power Project': ('detour-lake-power-project', 'Detour Lake Power Project (Class EA, 2010)'),
    '2010 DLTPP Class EA': ('detour-lake-transmission-class-ea', 'Detour Lake Transmission Power Project Class EA (2010)'),
    '2010 MNR Class EA': ('detour-lake-mine-mnr-class-ea', 'Detour Lake Project MNR Class EA (2010)'),
    '2011 CEAA Comprehensive Study DLM': ('detour-lake-ceaa-comprehensive-study', 'Detour Lake Gold Mine CEAA Comprehensive Study (2011)'),
    '2019 WDP Environmental Study Report': ('west-detour-esr', 'West Detour Project Environmental Study Report (2019)'),
    '2020 WDP Environmental Study Report Addendum': ('west-detour-esr', 'West Detour Project Environmental Study Report (2019)'),
    'Basic Impact Assessment': ('west-detour-esr', 'West Detour Project Environmental Study Report (2019)'),
}
# Never publish: review comments, responses, memos, notes -- anything that is
# not the original report. Moose Cree First Nation material in particular.
DETOUR_EXCLUDE = re.compile(r'Federal[_ ]Review|MCFN|Moose[_ ]Cree|Comment|Response|Consultation|'
                            r'SEA[_ ]review|\bMEM\b|\bNOTE\b|\bTAB\b|- Copy', re.I)

DETOUR_ROOT_FILES = {  # loose files at the folder root that are public record
    'MNR_West Detour Project-Statement of Completion_March_23_2021.pdf': 'west-detour-esr',
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
    groups = {}
    for sub, (s, name) in DETOUR_INCLUDE.items():
        d = os.path.join(DETOUR, sub)
        if not os.path.isdir(d):
            continue
        for p in sorted(glob.glob(os.path.join(d, '**', '*.pdf'), recursive=True)):
            base = os.path.basename(p)
            if DETOUR_EXCLUDE.search(base):
                continue
            groups.setdefault(s, {'name': name, 'docs': []})['docs'].append({
                'title': os.path.splitext(base)[0].replace('_', ' '), 'url': None,
                'category': sub, 'key': f'classea/detour-gold/{s}/{safe(sub)}/{safe(base)}',
                'local': p})
    for base, s in DETOUR_ROOT_FILES.items():
        p = os.path.join(DETOUR, base)
        if os.path.exists(p):
            groups.setdefault(s, {'name': s, 'docs': []})['docs'].append({
                'title': os.path.splitext(base)[0].replace('_', ' '), 'url': None,
                'category': 'Statement of Completion',
                'key': f'classea/detour-gold/{s}/{safe(base)}', 'local': p})
    meta = {
        'west-detour-esr': dict(
            proponent='Detour Gold Corporation (now Agnico Eagle Mines)',
            class_ea='Class EA for Activities of the MNDM under the Mining Act (ESR)',
            status='Statement of Completion issued March 23, 2021',
            description='West Detour Project: expansion of the Detour Lake Mine (West Detour pit, '
                        'North pit, Walter Lake overburden) assessed as a provincial ESR after the '
                        'federal Minister declined to designate the project under CEAA 2012 (2017).'),
        'detour-lake-mine-mnr-class-ea': dict(
            proponent='Detour Gold Corporation',
            class_ea='MNR Class EA for Resource Stewardship and Facility Development',
            status='Completed 2010'),
        'detour-lake-transmission-class-ea': dict(
            proponent='Detour Gold Corporation',
            class_ea='Class EA for Minor Transmission Facilities', status='Completed 2010'),
        'detour-lake-power-project': dict(
            proponent='Detour Gold Corporation',
            class_ea='Individual EA (Detour Lake Power Project, 230 kV line)',
            status='Approved December 15, 2010'),
        'detour-lake-ceaa-comprehensive-study': dict(
            proponent='Detour Gold Corporation',
            class_ea='CEAA 1992 Comprehensive Study (CEAR 10-03-52262)',
            status='Completed 2011'),
    }
    out = []
    for s, g in groups.items():
        m = meta.get(s, {})
        out.append({
            'slug': f'detour-gold-{s}', 'name': g['name'],
            'proponent': m.get('proponent'), 'jurisdiction': 'Ontario (Class EA)',
            'source': 'on_class_ea', 'class_ea': m.get('class_ea'),
            'type': 'Mining', 'status': m.get('status'), 'municipality': 'Cochrane',
            'registry_url': None, 'proponent_url':
                'https://www.agnicoeagle.com/English/operations/operations/detour-lake/',
            'description': m.get('description'), 'coords': DETOUR_COORDS,
            'docs': g['docs'],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    public = os.environ.get('R2_PUBLIC_BASE', '').rstrip('/')
    if not args.dry_run and not public:
        raise SystemExit('R2_PUBLIC_BASE is not set (export the R2_* variables first)')

    projects = hydro_one() + detour()
    known = load_manifest()
    all_docs = [(p, d) for p in projects for d in p['docs']]
    to_upload = [(p, d) for p, d in all_docs if d['local'] and not known.get(d['key'], {}).get('key')]
    missing = [(p, d) for p, d in all_docs if not d['local']]
    n_bytes = sum(os.path.getsize(d['local']) for _, d in to_upload)
    print(f'{len(projects)} projects, {len(all_docs)} documents: {len(to_upload)} to upload '
          f'({n_bytes / 1e9:.2f} GB), {len(all_docs) - len(to_upload) - len(missing)} already '
          f'archived, {len(missing)} listed but not on disk')
    if args.dry_run:
        for p, d in to_upload[:6]:
            print('  ', d['key'])
        return

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
            rec = seed.get(d['url'] or d['key']) or known.get(d['url'] or d['key']) or {}
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
