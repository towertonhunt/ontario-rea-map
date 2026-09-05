#!/usr/bin/env python3
"""Link Quebec REE projects to their BAPE dossiers by name.

Quebec's project registry (REE, ree.environnement.gouv.qc.ca) publishes no
documents; the record lives with the public-hearings office (BAPE) under a
dossier slug that carries no REE number. The two name the same project in
slightly different words, so match on distinctive tokens: the dossier's
title (from the harvest) and its slug against the REE project name,
accent-stripped, stop words dropped, and accept only an unambiguous best
match. Hand decisions in data/raw/qc_bape_overrides.json
({"<no_dossier>": "<slug>" or null}) win.

Writes data/raw/qc_bape_links.json {no_dossier: slug}; build_national_geojson
turns that into docs_path / doc_count on the REE feature.

  python3 scripts/link_qc_bape.py [--report]
"""
import json
import os
import re
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'data', 'raw')
CATALOGUE = os.path.join(RAW, 'qc_bape_doc_catalogue.json')
OVERRIDES = os.path.join(RAW, 'qc_bape_overrides.json')
OUT = os.path.join(RAW, 'qc_bape_links.json')
GEOJSON = os.path.join(ROOT, 'data', 'projects_canada.geojson')

# words that name the kind of project rather than the project: on their own
# they pair any wind farm with any other wind farm
STOP = set('''projet projets de du des la le les l d un une et a au aux en sur par pour
dans ville mrc municipalite programme construction implantation amenagement
agrandissement realisation travaux secteur territoire dossier bape quebec
region rue route km kv ligne poste parc eolien eoliens lieu enfouissement
technique sanitaire dragage entretien decennal quai quais berges centrale
mine minier miniere usine port chemin fer reconstruction prolongement
elargissement rehabilitation restauration installation installations
exploitation gisement production'''.split())


def tokens(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return {t for t in re.findall(r'[a-z0-9]+', s) if len(t) > 2 and t not in STOP}


def main():
    cat = json.load(open(CATALOGUE))
    overrides = json.load(open(OVERRIDES)) if os.path.exists(OVERRIDES) else {}
    dossiers = {}
    for slug, entry in cat.items():
        title = entry.get('title') if isinstance(entry, dict) else None
        toks = tokens(slug.replace('-', ' ')) | tokens(title)
        if toks:
            dossiers[slug] = toks

    g = json.load(open(GEOJSON))
    links, report = {}, []
    for f in g['features']:
        p = f['properties']
        if p.get('source') != 'qc_ree':
            continue
        m = re.search(r'no_dossier=([\w-]+)', p.get('registry_url') or '')
        if not m:
            continue
        nd = m.group(1)
        if nd in overrides:
            if overrides[nd]:
                links[nd] = overrides[nd]
            continue
        nt = tokens(p.get('name'))
        if not nt:
            continue
        scored = []
        for slug, dt in dossiers.items():
            inter = len(nt & dt)
            # two shared tokens is often just the place name (Sept-Îles,
            # Notre-Dame): accept it only when it is most of both names
            if inter >= 3 or (inter == 2 and inter / len(dt) >= 0.75 and inter / len(nt) >= 0.75):
                scored.append((inter / len(dt), inter / len(nt), slug))
        scored.sort(reverse=True)
        good = [x for x in scored if x[0] >= 0.5 and x[1] >= 0.5]
        verdict = 'none'
        if good and (len(good) == 1 or good[0][0] - good[1][0] >= 0.2
                     or good[0][1] - good[1][1] >= 0.2):
            links[nd] = good[0][2]
            verdict = 'matched'
        elif good:
            verdict = 'ambiguous'
        report.append((verdict, nd, p.get('name'), [x[2] for x in good[:3]]))

    json.dump(links, open(OUT, 'w'), ensure_ascii=False, indent=1, sort_keys=True)
    counts = {v: sum(1 for r in report if r[0] == v) for v in ('matched', 'ambiguous', 'none')}
    print(f'REE -> BAPE: {counts} (+{len([k for k in overrides if overrides[k]])} by override)'
          f' -> {OUT}')
    if '--report' in sys.argv:
        for verdict, nd, name, cands in report:
            if verdict != 'matched':
                print(f'  {verdict:9} {nd:12} {name[:60]!r} {cands}')


if __name__ == '__main__':
    main()
