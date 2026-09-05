#!/usr/bin/env python3
"""Build data/projects_canada.geojson from the raw registry harvests.

Sources merged:
  - projects.geojson            Ontario REA projects (existing, with doc libraries)
  - data/raw/rea_instruments.json     parsed REA instrument headings + issue dates
                                (scripts/fetch_rea_instruments.py) used to give
                                every Access Environment link a distinct name
  - data/raw/federal_layer*.geojson   IAAC assessment inventory (Completed / In progress / Terminated)
  - data/raw/bc_epic_projects.json    BC EAO project list (EPIC API)
  - data/raw/ns_ea_projects.html      Nova Scotia EA project table (no coordinates yet)
"""
import json
import re
import html as htmllib
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'data', 'raw')
OUT = os.path.join(ROOT, 'data', 'projects_canada.geojson')

features = []


def add(feat):
    p = feat['properties']
    if 'category' not in p:
        # Federal records put the EA *process* type ("Comprehensive study
        # under CEAA 1992", "Project on federal lands") in `type`, so a
        # type-only classify buckets every federal project as 'other'.
        # Fall back to the project name + description, which name the sector.
        cat = categorize(p.get('type') or '')
        if cat == 'other':
            cat = categorize(f"{p.get('name') or ''} {p.get('description') or ''}")
        p['category'] = cat
    features.append(feat)


CATEGORY_RULES = [
    ('wind', ['wind', 'éolien', 'eolien']),
    ('solar', ['solar', 'solaire']),
    ('biogas', ['biogas', 'anaerobic', 'biomass', 'bioenergy', 'biométhane',
                'biomethane', 'biomasse']),
    ('hydro', ['hydro', 'dam', 'water power', 'waterpower', 'barrage',
               'digue', 'rivière', 'riviere']),
    ('mining', ['mine', 'mining', 'quarry', 'aggregate', 'coal', 'metal',
                'minier', 'minière', 'miniere', 'carrière', 'carriere',
                "banc d'emprunt"]),
    ('oil_gas', ['oil', 'gas', 'lng', 'pipeline', 'petroleum', 'refinery',
                 'hydrocarbure', 'oléoduc', 'oleoduc', 'gazoduc',
                 'pétrolière', 'petroliere', 'gaz naturel']),
    ('nuclear', ['nuclear', 'uranium', 'nucléaire', 'nucleaire']),
    ('energy_other', ['energy', 'electric', 'transmission', 'power',
                      'énergie', 'energie', 'centrale']),
    ('transport', ['highway', 'road', 'rail', 'bridge', 'port', 'terminal',
                   'airport', 'ferry', 'transport', 'marine', 'routière',
                   'routiere', 'ferroviaire', 'aéroport', 'aeroport', 'quai']),
    ('water', ['water', 'wastewater', 'sewage', 'flood', 'irrigation',
               'reservoir', 'dredg', 'milieux humides', 'hydrique',
               'réservoir', 'dragage', 'eaux']),
    ('industrial', ['industrial', 'plant', 'facility', 'manufactur', 'pulp',
                    'mill', 'smelter', 'industrie', 'métallurgique',
                    'metallurgique', 'chimique', 'usine']),
    ('waste', ['waste', 'landfill', 'hazardous', 'matières résiduelles',
               'matieres residuelles', 'déchet', 'dechet', 'sols contaminés',
               'sols contamines']),
    ('agriculture', ['agricult', 'production animale', 'farm', 'élevage',
                     'elevage']),
    ('tourism', ['tourist', 'resort', 'récréotouristique', 'recreotouristique']),
]


# Keywords match at a word boundary (prefix), so stems still work
# ("manufactur"->manufacturing) but substrings don't ("port" no longer hits
# "report"/"important", "rail" no longer hits "trail"). A few short English
# words that are prefixes of unrelated words must match as whole words.
_WHOLE_WORD = {'dam', 'mill', 'port', 'oil', 'gas', 'rail', 'road', 'metal',
               'coal', 'mine', 'plant', 'mill', 'power', 'water', 'waste'}


def _kw_pattern(kw):
    # whole-word for ambiguous short words, else word-start (prefix) match
    return r'\b' + re.escape(kw) + (r'\b' if kw in _WHOLE_WORD else '')


_COMPILED = [(cat, re.compile('|'.join(_kw_pattern(k) for k in keys)))
             for cat, keys in CATEGORY_RULES]


def categorize(type_str):
    t = str(type_str).lower()
    for cat, pat in _COMPILED:
        if pat.search(t):
            return cat
    return 'other'



# ── Ontario REA instrument naming ────────────────────────────────────
# Access Environment publishes the approval and each later amendment as a
# separate instrument PDF, and the harvest titles them all "Renewable Energy
# Approval" - Marsh Hill Solar Farm shows five identical links. scripts/
# fetch_rea_instruments.py reads page one of each PDF for its heading and
# "Issue Date:"; here that becomes the display title:
#     2013-Apr-15 - Renewable Energy Approval
#     2014-May-21 - Renewable Energy Approval Amendment
REA_INSTRUMENTS = os.path.join(RAW, 'rea_instruments.json')
ACCESS_ENV = 'accessenvironment.ene.gov.on.ca/instruments/'
_MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def pretty_date(iso):
    """2013-04-15 -> 2013-Apr-15 (the format the site names documents in)."""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})$', iso or '')
    return f'{m.group(1)}-{_MON[int(m.group(2)) - 1]}-{m.group(3)}' if m else None


INSTRUMENT_KINDS = {'approval', 'amendment', 'revocation'}
GENERIC_TITLE = re.compile(r'^(download|pdf|click here|view|link|file)\b', re.I)


def name_rea_documents(feat, instruments):
    """Retitle a project's instrument links in place; return docs renamed.

    Only a document the parser positively identified - instrument heading
    plus an issue date - is renamed. Proponent-hosted PDFs that merely
    mention the REA (applications, modification reports, notices) keep
    their harvested titles."""
    n = 0
    sections = feat['properties'].get('doc_sections') or []
    # Developer-site harvests often list the same file twice - once with
    # its caption and once as the "Download" button, sometimes in another
    # section. Keep one copy per URL across the project, preferring the
    # caption and the section it was first filed under.
    keep = {}
    for sec in sections:
        for d in sec.get('docs') or []:
            u = d.get('url') or ''
            prev = keep.get(u)
            if prev is None or (GENERIC_TITLE.match(prev.get('title') or '')
                                and not GENERIC_TITLE.match(d.get('title') or '')):
                keep[u] = d
    for sec in sections:
        sec['docs'] = [d for d in sec.get('docs') or [] if keep.get(d.get('url') or '') is d]
    feat['properties']['doc_sections'] = [sec for sec in sections if sec['docs']]

    for sec in feat['properties']['doc_sections']:
        for d in sec['docs']:
            rec = instruments.get(d.get('url') or '')
            if not rec or rec.get('status') != 'ok':
                continue
            if rec.get('kind') not in INSTRUMENT_KINDS or not rec.get('issue_date'):
                continue
            date = pretty_date(rec['issue_date'])
            d['title'] = f"{date} - {rec['title']}"
            d['instrument_kind'] = rec['kind']
            d['date'] = rec['issue_date']
            if rec.get('rea_number'):
                d['rea_number'] = rec['rea_number']
            n += 1
        # Oldest first: the original approval, then amendments in sequence.
        # Anything without a date keeps its position at the end.
        sec['docs'].sort(key=lambda x: (x.get('date') is None, x.get('date') or ''))
        kinds = {x.get('instrument_kind') for x in sec['docs']}
        if kinds == {'approval'} or kinds == {'approval', 'amendment'}:
            if len(sec['docs']) > 1:
                sec['label'] = 'Renewable Energy Approval & amendments'
    # A proponent's signed copy of an instrument that Access Environment also
    # serves would otherwise land with the identical dated title. Mark the
    # copy so the two remain distinguishable and the registry copy reads as
    # the canonical one.
    named = [d for sec in feat['properties'].get('doc_sections') or []
             for d in sec['docs'] if d.get('instrument_kind')]
    registry = {(d['instrument_kind'], d['date']) for d in named
                if ACCESS_ENV in d['url']}
    for d in named:
        if ACCESS_ENV not in d['url'] and (d['instrument_kind'], d['date']) in registry:
            d['title'] += ' (proponent copy)'
    return n


# ── Ontario REA (existing map data, keep everything) ─────────────────
ont = json.load(open(os.path.join(ROOT, 'projects.geojson')))
rea_instruments = (json.load(open(REA_INSTRUMENTS))
                   if os.path.exists(REA_INSTRUMENTS) else {})
n_named = 0
for f in ont['features']:
    p = f['properties']
    p['jurisdiction'] = 'Ontario (REA)'
    p['source'] = 'ontario_rea'
    p['status'] = 'Approved'
    if rea_instruments:
        n_named += name_rea_documents(f, rea_instruments)
    add(f)
print(f'ontario REA: {len(ont["features"])} ({n_named} instrument links named'
      f' from {len(rea_instruments)} parsed instruments)')

# ── Federal IAAC inventory ───────────────────────────────────────────
LAYER_STATUS = {0: 'Completed', 1: 'In progress', 2: 'Terminated'}
n_fed = 0
for lid, status in LAYER_STATUS.items():
    path = os.path.join(RAW, f'federal_layer{lid}.geojson')
    if not os.path.exists(path):
        continue
    d = json.load(open(path))
    for f in d['features']:
        p = f.get('properties') or {}
        geom = f.get('geometry')
        if not geom or geom.get('coordinates') in (None, [None, None]):
            continue
        name = (p.get('ProjectName') or p.get('Name') or p.get('NAME') or
                p.get('project_name') or p.get('Title') or 'Unnamed project')
        url = (p.get('RegistryLink') or p.get('URL') or p.get('Link') or
               p.get('registry_url') or None)
        props = {
            'name': str(name).strip(),
            'jurisdiction': 'Federal (IAAC)',
            'source': 'federal_iaac',
            'status': status,
            'type': (p.get('ProjectType') or p.get('Type') or 'other'),
            'proponent': p.get('Proponent') or p.get('proponent'),
            'registry_url': url,
            'raw': {k: v for k, v in p.items() if v not in (None, '')},
        }
        add({'type': 'Feature', 'geometry': geom, 'properties': props})
        n_fed += 1
print(f'federal: {n_fed}')

# ── BC EPIC ──────────────────────────────────────────────────────────
bc_path = os.path.join(RAW, 'bc_epic_projects.json')
n_bc = 0
if os.path.exists(bc_path):
    bc_docs = {}
    bc_cat = os.path.join(RAW, 'bc_doc_catalogue.json')
    if os.path.exists(bc_cat):
        bc_docs = {pid: len(v['docs']) for pid, v in json.load(open(bc_cat)).items()}
    bc = json.load(open(bc_path))[0]['searchResults']
    for p in bc:
        c = p.get('centroid') or []
        try:
            lon, lat = float(c[0]), float(c[1])
        except (ValueError, TypeError, IndexError):
            continue
        dec = p.get('eacDecision')
        if isinstance(dec, dict):
            dec = dec.get('name')
        props = {
            'name': p.get('name'),
            'jurisdiction': 'British Columbia (EAO)',
            'source': 'bc_epic',
            'status': p.get('status') or p.get('eaStatus'),
            'type': p.get('type') or 'other',
            'proponent': (p.get('proponent') or {}).get('name')
                         if isinstance(p.get('proponent'), dict) else p.get('proponent'),
            'decision': dec,
            'region': p.get('region'),
            'location': p.get('location'),
            'registry_url': 'https://projects.eao.gov.bc.ca/p/' + p['_id']
                            if p.get('_id') else None,
            'description': (p.get('description') or '')[:400],
        }
        if p.get('_id') in bc_docs:
            props['doc_count'] = bc_docs[p['_id']]
            props['docs_path'] = f"data/docs/bc/{p['_id']}.json"
        add({'type': 'Feature',
             'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
             'properties': props})
        n_bc += 1
print(f'bc: {n_bc}')

# ── Federal IAAC (item index via exploration list API) ───────────────
import gzip as _gzip
FED_ARCHIVE_BASE = 'https://iaac-aeic.gc.ca/archives/evaluations'
NS_EA_BASE = 'https://novascotia.ca/nse/ea/'

listgz = os.path.join(RAW, 'federal_list_all.json.gz')
fed_seen = set()
n_fedlist = 0
fed_docdir = os.path.join(ROOT, 'data', 'docs', 'federal')
if os.path.exists(listgz):
    for e in json.load(_gzip.open(listgz, 'rt')):
        if e.get('document_type') not in ('project', 'archive-project'):
            continue
        pid = e.get('project_id')
        if not pid or pid in fed_seen:
            continue
        if e.get('document_type') == 'archive-project' and not e.get('relative_path'):
            continue  # no way to build a working link into the archive
        fed_seen.add(pid)
        geom = None
        if 'lat' in e and 'lon' in e:
            try:
                lat, lon = float(e['lat']), float(e['lon'])
                # Canada bbox: southern tip (Middle Island) is ~41.7 N, so
                # 40 let US facilities through (e.g. a Salt Lake City plant).
                if 41.5 < lat < 84 and -141.5 < lon < -52:
                    geom = {'type': 'Point', 'coordinates': [lon, lat]}
            except (TypeError, ValueError):
                pass
        # Archived (pre-CEAA-2012) records are not served by the live registry
        # under /050/evaluations/proj/<id> -- that 404s. They live as static
        # captures under /archives/evaluations + the index's relative_path.
        archived = e.get('document_type') == 'archive-project'
        if archived:
            registry_url = FED_ARCHIVE_BASE + e['relative_path'].replace('\\', '/')
        else:
            registry_url = f'https://iaac-aeic.gc.ca/050/evaluations/proj/{pid}'
        add({'type': 'Feature', 'geometry': geom, 'properties': {
            'name': e.get('project_name_en') or f'Federal project {pid}',
            'jurisdiction': 'Federal (IAAC)',
            'source': 'federal_iaac',
            'status': e.get('status_en'),
            'type': e.get('ea_type_en') or 'other',
            'ea_type': e.get('ea_type_en'),
            'ea_phase': e.get('ea_phase_en'),
            'proponent': e.get('proponent_en'),
            'province_codes': e.get('province_en'),
            'location': e.get('location_en'),
            'description': e.get('description'),
            'archived': True if archived else None,
            'registry_url': registry_url,
        }})
        dp = os.path.join(fed_docdir, f'{pid}.json')
        if os.path.exists(dp):
            props = features[-1]['properties']
            props['doc_count'] = len(json.load(open(dp))['docs'])
            props['docs_path'] = f'data/docs/federal/{pid}.json'
        n_fedlist += 1
print(f'federal (list index): {n_fedlist}')

# ── Federal IAAC (full inventory via exploration api-map) ────────────
apimap = os.path.join(RAW, 'federal_apimap.geojson')
n_fed2 = 0
if os.path.exists(apimap):
    d = json.load(open(apimap))
    for f in d['features']:
        p = f.get('properties') or {}
        geom = f.get('geometry')
        # normalize MultiPoint -> Point (first location)
        if geom and geom.get('type') == 'MultiPoint' and geom.get('coordinates'):
            geom = {'type': 'Point', 'coordinates': geom['coordinates'][0]}
        pid = p.get('project_id')
        if pid and pid in fed_seen:
            continue  # already added from the richer list index
        props = {
            'name': p.get('project_name_en') or p.get('project_name_fr') or 'Unnamed',
            'jurisdiction': 'Federal (IAAC)',
            'source': 'federal_iaac',
            'status': p.get('project_state_en'),
            'type': p.get('project_cat_en') or 'other',
            'proponent': p.get('proponent_en'),
            'location': p.get('location_en'),
            'province_codes': p.get('province_codes'),
            'description': (p.get('description_en') or '')[:400],
            'registry_url': f'https://iaac-aeic.gc.ca/050/evaluations/proj/{pid}'
                            if pid else p.get('project_url_en'),
        }
        add({'type': 'Feature', 'geometry': geom, 'properties': props})
        n_fed2 += 1
print(f'federal (api-map): {n_fed2}')

# ── Ontario provincial EAs (Individual/Comprehensive, by sector) ─────
# Preferred source: the per-project pages harvested by fetch_on_ea_pages.py,
# which carry proponent, status, location and the documentation links. The
# category listing below is the fallback when that harvest hasn't been run --
# it yields a name and a URL and nothing else.
on_pages = os.path.join(RAW, 'on_ea_project_pages.json')
on_cat = os.path.join(RAW, 'on_ea_projects_category.html')
n_onp = 0
if os.path.exists(on_pages):
    on_docdir = os.path.join(ROOT, 'data', 'docs', 'on')
    for rec in json.load(open(on_pages)):
        props = {
            'name': rec.get('name') or rec['slug'],
            'jurisdiction': 'Ontario (Provincial EA)',
            'source': 'on_provincial_ea',
            'status': rec.get('status'),
            'type': rec.get('sector') or 'Other',
            'ea_type': rec.get('ea_sector'),
            'proponent': rec.get('proponent'),
            'location': rec.get('location'),
            'municipality': rec.get('municipality'),
            'reference_number': rec.get('reference_number'),
            'decision_date': rec.get('decision_date'),
            'proponent_url': rec.get('proponent_url'),
            'registry_url': rec['url'],
        }
        dp = os.path.join(on_docdir, f"{rec['slug']}.json")
        if os.path.exists(dp):
            props['doc_count'] = len(rec['docs'])
            props['docs_path'] = f"data/docs/on/{rec['slug']}.json"
        add({'type': 'Feature', 'geometry': None, 'properties': props})
        n_onp += 1
elif os.path.exists(on_cat):
    h = open(on_cat, encoding='utf-8', errors='replace').read()
    SECTIONS = ['Electricity', 'Mining', 'Forestry', 'Municipal infrastructure',
                'Waste management', 'Transit', 'Transportation', 'Other']
    for part in re.split(r'<h2[^>]*>', h):
        mt = re.match(r'\s*([^<]+)</h2>(.*)', part, re.S)
        if not mt or mt.group(1).strip() not in SECTIONS:
            continue
        sector = mt.group(1).strip()
        for u, t in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', mt.group(2), re.S):
            name = re.sub(r'\s+', ' ',
                          htmllib.unescape(re.sub(r'<[^>]+>', ' ', t))).strip()
            # skip guides/reference pages mixed into the lists
            if (not name or 'back to top' in name.lower()
                    or name.lower().startswith(('guide', 'environmental assessment requirement'))):
                continue
            if u.startswith('/'):
                u = 'https://www.ontario.ca' + u
            props = {
                'name': name,
                'jurisdiction': 'Ontario (Provincial EA)',
                'source': 'on_provincial_ea',
                'status': None,
                'type': sector,
                'proponent': None,
                'registry_url': u,
            }
            add({'type': 'Feature', 'geometry': None, 'properties': props})
            n_onp += 1
print(f'ontario provincial EA (no coords yet): {n_onp}')

# ── Ontario Class EA projects (proponent-held records; seed_classea_local.py) ──
# Hydro One transmission Class EAs and the Detour Lake / West Detour record:
# documents that no registry ever received, mirrored to the archive.
classea_path = os.path.join(RAW, 'classea_projects.json')
n_classea = 0
if os.path.exists(classea_path):
    for rec in json.load(open(classea_path)):
        geom = None
        if rec.get('coords'):
            lon, lat = rec['coords']
            geom = {'type': 'Point', 'coordinates': [lon, lat]}
        props = {k: v for k, v in rec.items() if k not in ('coords', 'slug')}
        props.setdefault('category', 'energy_other' if 'ransmission' in (rec.get('type') or '')
                         else 'mining' if rec.get('type') == 'Mining' else 'other')
        add({'type': 'Feature', 'geometry': geom, 'properties': props})
        n_classea += 1
print(f'ontario class EA (proponent-held): {n_classea}')

# ── Proponent EA document libraries (promote_proponent_docs.py) ──────
# Documents found on proponent sites that could not be tied to a specific
# project become one searchable feature per proponent (no geometry).
libs_path = os.path.join(RAW, 'proponent_libraries.json')
n_libs = 0
if os.path.exists(libs_path):
    for lib in json.load(open(libs_path)):
        add({'type': 'Feature', 'geometry': None, 'properties': {
            'name': lib['name'], 'jurisdiction': 'Proponent website',
            'source': 'proponent_site', 'proponent': lib['proponent'],
            'type': 'Document library', 'status': None,
            'registry_url': lib.get('website'), 'proponent_url': lib.get('website'),
            'doc_count': lib['doc_count'], 'docs_path': lib['docs_path'],
            'category': 'other',
        }})
        n_libs += 1
print(f'proponent document libraries: {n_libs}')

# ── Quebec REE (table + coordinates scraped from carte.asp pages) ────
qc_path = os.path.join(RAW, 'qc_ree_resultats.html')
n_qc = 0
if os.path.exists(qc_path):
    coords = {}
    cpath = os.path.join(RAW, 'qc_coords.json')
    if os.path.exists(cpath):
        coords = json.load(open(cpath))
    # REE publishes no documents; the record is the BAPE dossier that
    # scripts/link_qc_bape.py matched by name (data/raw/qc_bape_links.json)
    bape_links = {}
    lpath = os.path.join(RAW, 'qc_bape_links.json')
    if os.path.exists(lpath):
        bape_links = json.load(open(lpath))
    qc_docdir = os.path.join(ROOT, 'data', 'docs', 'qc')
    n_qc_linked = 0
    h = open(qc_path, encoding='utf-8', errors='replace').read()
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', h, re.S)
    for r in rows[1:]:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.S)
        if len(cells) < 5:
            continue
        strip = lambda c: re.sub(r'\s+', ' ',
                                 htmllib.unescape(re.sub(r'<[^>]+>', ' ', c))).strip()
        name, prop, sector, muni, updated = (strip(c) for c in cells[:5])
        if not name or name.startswith('Nom du projet'):
            continue
        dm = re.search(r'no_dossier=([^"&]+)', r)
        dossier = dm.group(1) if dm else None
        geom = None
        if dossier and dossier in coords:
            lat, lon = coords[dossier]
            # sanity: QC latitudes 44..63, longitudes -80..-57
            if 40 < lat < 65 and -85 < lon < -50:
                geom = {'type': 'Point', 'coordinates': [lon, lat]}
        props = {
            'name': name,
            'jurisdiction': 'Quebec (MELCCFP)',
            'source': 'qc_ree',
            'status': None,
            'type': sector,
            'proponent': prop,
            'municipality': muni,
            'updated': updated,
            'registry_url': ('https://www.ree.environnement.gouv.qc.ca/projet.asp?no_dossier=' + dossier)
                            if dossier else None,
        }
        slug = bape_links.get(dossier)
        if slug:
            cpath = os.path.join(qc_docdir, f'{slug[:60]}.json')
            if os.path.exists(cpath):
                try:
                    n_docs = len(json.load(open(cpath)).get('docs') or [])
                except (ValueError, OSError):
                    n_docs = 0
                if n_docs:
                    props['docs_path'] = f'data/docs/qc/{slug[:60]}.json'
                    props['doc_count'] = n_docs
                    props['bape_url'] = f'https://www.bape.gouv.qc.ca/fr/dossiers/{slug}/'
                    n_qc_linked += 1
        add({'type': 'Feature', 'geometry': geom, 'properties': props})
        n_qc += 1
print(f'quebec: {n_qc} ({n_qc_linked} linked to a BAPE dossier)')

# ── Nova Scotia (no coordinates in source; parsed for list/search) ───
ns_path = os.path.join(RAW, 'ns_ea_projects.html')
n_ns = 0
ns_docs = {}
ns_cat_path = os.path.join(RAW, 'ns_doc_catalogue.json')


def _ns_key(url):
    """The catalogue and the project list disagree on 'www.' and trailing
    slashes; match on the slug so both spellings find their documents."""
    return url.rstrip('/').rsplit('/', 1)[-1].replace('.asp', '').lower()


if os.path.exists(ns_cat_path):
    for url, docs in json.load(open(ns_cat_path)).items():
        ns_docs[_ns_key(url)] = len(docs)
if os.path.exists(ns_path):
    h = open(ns_path, encoding='utf-8', errors='replace').read()
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', h, re.S)
    for r in rows[1:]:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.S)
        if len(cells) < 3:
            continue
        link = re.search(r'href="([^"]+)"', cells[0])
        strip = lambda c: re.sub(r'\s+', ' ',
                                 htmllib.unescape(re.sub(r'<[^>]+>', ' ', c))).strip()
        name, prop, date = strip(cells[0]), strip(cells[1]), strip(cells[2])
        if not name or name == 'NAME':
            continue
        url = link.group(1) if link else None
        if url and url.startswith('/'):
            url = 'https://novascotia.ca' + url
        elif url and not url.startswith('http'):
            # some rows link with a bare slug ('harbour-hills-wind/'),
            # which the map rendered as a dead relative link
            url = NS_EA_BASE + url
        props = {
            'name': name,
            'jurisdiction': 'Nova Scotia (NSECC)',
            'source': 'ns_ea',
            'status': None,
            'type': 'other',
            'proponent': prop,
            'date': date,
            'registry_url': url,
        }
        if url and _ns_key(url) in ns_docs:
            props['doc_count'] = ns_docs[_ns_key(url)]
            slug = url.rstrip('/').rsplit('/', 1)[-1].replace('.asp', '') or 'index'
            props['docs_path'] = f'data/docs/ns/{slug}.json'
        # No coordinates published; geometry null keeps them list-searchable
        add({'type': 'Feature', 'geometry': None, 'properties': props})
        n_ns += 1
print(f'nova scotia (no coords yet): {n_ns}')

# ── Newfoundland & Labrador (full registration list) ─────────────────
nl_path = os.path.join(RAW, 'nl_ea_list.html')
n_nl = 0
if os.path.exists(nl_path):
    h = open(nl_path, encoding='utf-8', errors='replace').read()
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', h, re.S)[1:]:
        # NL rows use unclosed <td> tags — split on the opening tag
        cells = re.split(r'<td[^>]*>', r)[1:]
        if len(cells) < 4:
            continue
        strip = lambda c: re.sub(r'\s+', ' ',
                                 htmllib.unescape(re.sub(r'<[^>]+>', ' ', c))).strip()
        reg, title_prop, registered, status = (strip(c) for c in cells[:4])
        released = strip(cells[4]) if len(cells) > 4 else None
        if not reg or not reg[0].isdigit():
            continue
        m = re.match(r'(.*?)\s*Proponent:\s*(.*)', title_prop)
        name, proponent = (m.group(1), m.group(2)) if m else (title_prop, None)
        link = re.search(r'href="([^"]+)"', cells[1] if len(cells) > 1 else '')
        url = link.group(1) if link else None
        if url and url.startswith('/'):
            url = 'https://www.gov.nl.ca' + url
        add({'type': 'Feature', 'geometry': None, 'properties': {
            'name': name, 'jurisdiction': 'Newfoundland & Labrador (ECC)',
            'source': 'nl_ea', 'status': status, 'type': name,
            'proponent': proponent, 'reg_number': reg,
            'date': registered, 'release_date': released,
            'registry_url': url or f'https://www.gov.nl.ca/eccc/projects/projects-{reg}/',
        }})
        n_nl += 1
print(f'newfoundland (no coords yet): {n_nl}')

# ── Ontario abandoned mines (AMIS) ───────────────────────────────────
amis_path = os.path.join(RAW, 'on_amis_parsed.json')
n_amis = 0
if os.path.exists(amis_path):
    for e in json.load(open(amis_path)):
        add({'type': 'Feature',
             'geometry': {'type': 'Point', 'coordinates': [e['lon'], e['lat']]},
             'properties': {
                 'name': (e.get('name') or f"AMIS {e.get('amis_id')}").title(),
                 'jurisdiction': 'Ontario (Abandoned Mines)',
                 'source': 'on_amis', 'category': 'mining',
                 'status': (e.get('status') or '').title() or None,
                 'type': e.get('commodity') or 'mining',
                 'commodity': e.get('commodity'),
                 'closure_plan': e.get('closure_plan'),
                 'classification': e.get('classification'),
                 'municipality': (e.get('township') or '').title() or None,
                 'registry_url': ('https://www.geologyontario.mines.gov.on.ca/'
                                  f"persistent-linking?abandoned-mine={e['amis_id']}")
                                 if e.get('amis_id') else None,
             }})
        n_amis += 1
print(f'ontario abandoned mines: {n_amis}')

# ── Manitoba (Environment Act registry, parsed by agent) ─────────────
mb_path = os.path.join(RAW, 'mb_ea_parsed.json')
n_mb = 0
if os.path.exists(mb_path):
    for e in json.load(open(mb_path)):
        add({'type': 'Feature', 'geometry': None, 'properties': {
            'name': e.get('name') or f"File {e.get('number')}",
            'jurisdiction': 'Manitoba (Environment Act)',
            'source': 'mb_ea', 'status': None, 'type': e.get('name') or '',
            'proponent': e.get('proponent'), 'file_number': e.get('number'),
            'date': e.get('date'), 'registry_url': e.get('url'),
        }})
        n_mb += 1
print(f'manitoba (no coords yet): {n_mb}')

# ── Gazetteer geocode pass for sources without coordinates ───────────
# GeoNames admin1 codes; match municipality field first, then place names
# embedded in the project name (n-grams, longest first). Conservative on
# single words to avoid false pins; every geocoded feature is flagged so
# the UI can render it as approximate.
GAZ = os.path.join(ROOT, 'data', 'geo', 'ca_places.json')
ADMIN1 = {
    'Manitoba (Environment Act)': '03',
    'Newfoundland & Labrador (ECC)': '05',
    'Nova Scotia (NSECC)': '07',
    'Ontario (Provincial EA)': '08',
    'Ontario (Class EA)': '08',
    'Quebec (MELCCFP)': '10',
}
STOP = {'project', 'projet', 'wind', 'solar', 'farm', 'energy', 'power',
        'mine', 'mining', 'quarry', 'centre', 'center', 'development',
        'expansion', 'extension', 'plant', 'facility', 'station', 'system',
        'road', 'highway', 'bridge', 'trail', 'phase', 'limited', 'company',
        'waste', 'water', 'sewage', 'treatment', 'landfill', 'lagoon',
        'transmission', 'pipeline', 'terminal', 'operation', 'operations',
        'aggregate', 'gravel', 'peat', 'forest', 'forestry', 'hydro',
        'control', 'management', 'upgrade', 'replacement', 'removal',
        'construction', 'municipal', 'regional', 'provincial', 'national'}
if os.path.exists(GAZ):
    gaz = json.load(open(GAZ))
    n_geo = 0
    for f in features:
        if f.get('geometry') is not None:
            continue
        p = f['properties']
        a1 = ADMIN1.get(p.get('jurisdiction'))
        if not a1:
            continue
        hit = None
        muni = (p.get('municipality') or '').lower().strip()
        if muni and f'{muni}|{a1}' in gaz:
            hit = (gaz[f'{muni}|{a1}'], 'municipality')
        if not hit:
            # "RM of X" / "Town of X" in proponent or name is a municipality
            blob = f"{p.get('proponent') or ''} | {p.get('name') or ''}"
            for m in re.finditer(
                    r"\b(?:R\.?M\.?|Rural Municipality|Town|City|Village|LGD|"
                    r"Local Government District|Municipality) of "
                    r"([A-Z][A-Za-z.'’ -]+)", blob):
                # try full match, then progressively shorter pieces
                # ("Glenboro-South Cypress" -> "Glenboro")
                cand = m.group(1).strip().lower()
                parts = re.split(r'[-–]| and ', cand)
                for c in [cand] + [q.strip() for q in parts if q.strip()]:
                    c = re.sub(r'\s+(no\.?|#)\s*\d+$', '', c).strip(" .'’-")
                    if f'{c}|{a1}' in gaz:
                        hit = (gaz[f'{c}|{a1}'], f'municipal_pattern:{c}')
                        break
                if hit:
                    break
        if not hit:
            words = [w for w in re.findall(r"[a-zà-ÿ'’-]+",
                                           (p.get('name') or '').lower())]
            grams = []
            for n in (4, 3, 2):
                grams += [' '.join(words[i:i+n])
                          for i in range(len(words) - n + 1)]
            grams += [w for w in words if len(w) >= 6 and w not in STOP]
            for g in grams:
                if f'{g}|{a1}' in gaz:
                    hit = (gaz[f'{g}|{a1}'], f'name:{g}')
                    break
        if hit:
            (lat, lon), how = hit
            f['geometry'] = {'type': 'Point', 'coordinates': [lon, lat]}
            p['geocode'] = 'approximate'
            p['geocode_match'] = how
            n_geo += 1
    print(f'gazetteer geocoded: {n_geo}')

# ── Proponent documents matched to existing projects ────────────────
# promote_proponent_docs.py maps feature keys (source|name) to documents
# found on the proponent's site. Merge them into the project's catalogue
# (idempotent by url); create a catalogue when the project had none.
pidx_path = os.path.join(RAW, 'proponent_docs_index.json')
if os.path.exists(pidx_path):
    pidx = json.load(open(pidx_path))
    pdocdir = os.path.join(ROOT, 'data', 'docs', 'proponent')
    os.makedirs(pdocdir, exist_ok=True)
    n_merged = n_feats = 0
    for f in features:
        p = f['properties']
        docs = pidx.get(f"{p.get('source')}|{p.get('name')}")
        if not docs:
            continue
        if p.get('docs_path'):
            cpath = os.path.join(ROOT, p['docs_path'])
        else:
            slug = re.sub(r'[^a-z0-9]+', '-', (p.get('name') or '').lower())[:80]
            cpath = os.path.join(pdocdir, f"{p.get('source')}-{slug}.json")
            p['docs_path'] = f"data/docs/proponent/{p.get('source')}-{slug}.json"
        try:
            cat = json.load(open(cpath))
        except (OSError, ValueError):
            cat = {'project': p.get('name'), 'docs': []}
        have = {d.get('url') for d in cat['docs']}
        new_docs = [d for d in docs if d['url'] not in have]
        if new_docs:
            cat['docs'] += new_docs
            json.dump(cat, open(cpath, 'w'), ensure_ascii=False)
        p['doc_count'] = len(cat['docs'])
        n_merged += len(new_docs)
        n_feats += 1
    print(f'proponent docs merged into projects: {n_merged} new across {n_feats} features')

# ── Inventory enrichment ────────────────────────────────────────────
# Backfill proponent/coords from external major-project inventories for
# features whose registry publishes neither (e.g. Ontario provincial EA
# is name+url only, which left Waasigan unpinned and Hydro One
# unsearchable). File produced by scripts/enrich_from_inventories.py;
# strict bidirectional matching, so fills are trusted but flagged.
enr_path = os.path.join(RAW, 'inventory_enrichment.json')
if os.path.exists(enr_path):
    enr = json.load(open(enr_path))
    n_p = n_c = 0
    for f in features:
        p = f['properties']
        rec = enr.get(f"{p.get('jurisdiction')}||{p.get('name')}")
        if not rec:
            continue
        if rec.get('proponent') and not p.get('proponent'):
            p['proponent'] = rec['proponent']
            p['proponent_source'] = rec['source']
            n_p += 1
        if rec.get('coords') and not f.get('geometry'):
            lon, lat = rec['coords']
            if 41.5 < lat < 84 and -141.5 < lon < -52:
                f['geometry'] = {'type': 'Point', 'coordinates': [lon, lat]}
                p['geocode'] = 'inventory'
                n_c += 1
    print(f'inventory enrichment: {n_p} proponents, {n_c} coordinates')

# ── Gap overlay (opt-in layer, like AMIS) ───────────────────────────
# Majors that external inventories list but no registry we harvest has —
# pinned so gaps are visible on the map, not buried in gap_report.json.
gap_path = os.path.join(ROOT, 'data', 'gap_report.json')
if os.path.exists(gap_path):
    seen_gap = set()
    n_gap = 0
    for x in json.load(open(gap_path)).get('results', []):
        ext = x.get('ext') or {}
        c = ext.get('coords')
        key = (ext.get('name') or '').strip().lower()
        if x.get('verdict') != 'gap' or not c or not key or key in seen_gap:
            continue
        lon, lat = c
        if not (41.5 < lat < 84 and -141.5 < lon < -52):
            continue
        seen_gap.add(key)
        n_gap += 1
        add({'type': 'Feature',
             'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
             'properties': {
                 'id': f'nrcangap-{n_gap}',
                 'name': ext.get('name'),
                 'type': ext.get('sector') or 'Major project (inventory)',
                 'proponent': ext.get('proponent'),
                 'status': ext.get('status'),
                 'jurisdiction': 'Major projects inventory (unmatched)',
                 'source': 'nrcan_gap', 'geocode': 'inventory',
                 'note': ('Listed in an external major-projects inventory; '
                          'no matching record found in the EA registries '
                          'this map harvests.')}})
    print(f'gap overlay: {n_gap} unmatched inventory majors pinned')

json.dump({'type': 'FeatureCollection', 'features': features}, open(OUT, 'w'))
n_geom = sum(1 for f in features if f.get('geometry'))
print(f'TOTAL: {len(features)} ({n_geom} mappable) -> {OUT}')
