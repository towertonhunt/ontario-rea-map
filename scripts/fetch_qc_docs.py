#!/usr/bin/env python3
"""Harvest every document in every BAPE dossier, with its code and date.

The BAPE (Quebec's public-hearings office) lists a dossier's documents at
bape.gouv.qc.ca/fr/dossiers/<slug>/documentation/, 25 per page, ordered by
"cote". Each tile carries the document's cote (RP1, DA3, CM12 ...), its
date in French ("29 février 1992", or "s. d." when undated), page count,
format and size, the author, and the download link into the voûte
(voute.bape.gouv.qc.ca/dl/?id=<n>). The July 2026 harvest read page one
only, so it held ~7,300 of an estimated ~57,000 documents.

Writes data/raw/qc_bape_doc_catalogue.json:

  {slug: {"title": dossier title, "pages": n,
          "docs": [{title, url, code, date, author, pages, format, size_mb}]}}

Resumable: a dossier whose page count has not changed since the last run
is skipped unless --force. Paced at ~1.4 s/page; a full run is ~1 h.

  python3 scripts/fetch_qc_docs.py [--limit N] [--slug S] [--force]

Each run first walks the dossier listing to pick up new dossiers.
"""
import argparse
import html as H
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'data', 'raw')
SLUGS = os.path.join(RAW, 'qc_bape_slugs.json')
LISTING = os.path.join(RAW, 'qc_bape_dossiers.html')
OUT = os.path.join(RAW, 'qc_bape_doc_catalogue.json')
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36')
PACE = 0.4

MONTHS = {m: i for i, m in enumerate(
    ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
     'septembre', 'octobre', 'novembre', 'décembre'], 1)}
MONTHS.update({'fevrier': 2, 'aout': 8, 'decembre': 12})

TILE_RX = re.compile(r'<div class="m-document-tile">(.*?)</div>\s*</div>', re.S)
CODE_RX = re.compile(r'a-title-regular--700">\s*([^<]+?)\s*<')
META_RX = re.compile(r'\(\s*((?:<span[^>]*>[^<]*</span>\s*,?\s*)+)\)', re.S)
LINK_RX = re.compile(r'href="(https://voute\.bape\.gouv\.qc\.ca/dl/\?id=[^"]+)"[^>]*>(.*?)</a>', re.S)
AUTHOR_RX = re.compile(r'm-document-tile__author">\s*(?:Auteur\s*:\s*)?(.*?)\s*</span>', re.S)
PAGE_RX = re.compile(r'[?&]page=(\d+)')
# the breadcrumb's current item is "Documentation du dossier"; the dossier
# title is the item before it, the link back to /fr/dossiers/<slug>/
TITLE_RX = re.compile(r'href="/fr/dossiers/[^/"]+/"[^>]*>\s*<span itemprop="name">(.*?)</span>', re.S)
THEME_RX = re.compile(r'o-dossier-header__theme">(.*?)<|m-dossier-tile__theme">(.*?)<', re.S)


def text(s):
    return ' '.join(H.unescape(re.sub(r'<[^>]+>', ' ', s)).split())


def get(url, tries=3):
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            return urllib.request.urlopen(req, timeout=60).read().decode('utf-8', 'replace')
        except Exception:                                     # noqa: BLE001
            if t == tries - 1:
                raise
            time.sleep(6 * (t + 1))


def parse_date(s):
    """'29 février 1992' -> 1992-02-29; '1992' -> 1992; 's. d.' -> None."""
    s = s.strip().lower().replace('1er', '1')
    m = re.match(r'(\d{1,2})\s+([a-zéû]+)\s+(\d{4})$', s)
    if m and m.group(2) in MONTHS:
        return f'{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}'
    m = re.match(r'([a-zéû]+)\s+(\d{4})$', s)
    if m and m.group(1) in MONTHS:
        return f'{m.group(2)}-{MONTHS[m.group(1)]:02d}'
    m = re.match(r'(\d{4})$', s)
    return m.group(1) if m else None


def parse_tiles(page_html):
    docs = []
    for tile in TILE_RX.findall(page_html):
        link = LINK_RX.search(tile)
        if not link:
            continue
        code = CODE_RX.search(tile)
        author = AUTHOR_RX.search(tile)
        d = {'title': text(link.group(2))[:300] or 'Document', 'url': link.group(1),
             'code': code.group(1).strip() if code else None,
             'date': None, 'author': text(author.group(1))[:120] if author else None}
        meta = META_RX.search(tile)
        if meta:
            parts = [text(p) for p in re.findall(r'<span[^>]*>([^<]*)</span>', meta.group(1))]
            for p in parts:
                if d['date'] is None and (parse_date(p) or p.lower().startswith('s. d')):
                    d['date'] = parse_date(p)
                    d['date_text'] = p
                elif re.search(r'\bpages?\b', p):
                    d['pages'] = p
                elif re.fullmatch(r'[\d.,]+\s*(Mo|Ko|Go)', p, re.I):
                    d['size'] = p
                elif re.fullmatch(r'[a-z0-9]{2,5}', p, re.I):
                    d['format'] = p.lower()
        # a tile can carry extra links (annexes): keep every distinct one
        for extra in LINK_RX.findall(tile)[1:]:
            docs.append({**d, 'title': text(extra[1])[:300] or d['title'], 'url': extra[0]})
        docs.append(d)
    return docs


def dossier_titles():
    if not os.path.exists(LISTING):
        return {}
    h = open(LISTING, encoding='utf-8', errors='replace').read()
    out = {}
    for slug, inner in re.findall(r'href="/fr/dossiers/([^/"]+)/"[^>]*>(.*?)</a>', h, re.S):
        t = text(inner)
        if t and slug not in out:
            out[slug] = t
    return out


def refresh_slugs():
    """The dossier listing is paginated (16 per page); union every slug it
    shows into data/raw/qc_bape_slugs.json so new dossiers get harvested."""
    slugs = json.load(open(SLUGS)) if os.path.exists(SLUGS) else []
    known = set(slugs)
    first = get('https://www.bape.gouv.qc.ca/fr/dossiers/?page=1')
    n_pages = max([int(x) for x in PAGE_RX.findall(first)] or [1])
    pages = [first]
    for p in range(2, n_pages + 1):
        time.sleep(PACE)
        pages.append(get(f'https://www.bape.gouv.qc.ca/fr/dossiers/?page={p}'))
    new = []
    for h in pages:
        for slug in re.findall(r'href="/fr/dossiers/([^/"?]+)/"', h):
            if slug not in known:
                known.add(slug)
                new.append(slug)
    if new:
        json.dump(slugs + new, open(SLUGS, 'w'), ensure_ascii=False, indent=0)
    print(f'dossier listing: {n_pages} pages, {len(new)} new slugs -> {len(slugs) + len(new)} total')
    return slugs + new


def harvest(slug, known, force):
    base = f'https://www.bape.gouv.qc.ca/fr/dossiers/{slug}/documentation/'
    first = get(base + '?order=cote%3Aasc')
    n_pages = max([int(x) for x in PAGE_RX.findall(first)] or [1])
    prev = known.get(slug)
    if prev and not force and prev.get('pages') == n_pages and prev.get('docs'):
        return prev, 0
    title = TITLE_RX.search(first)
    docs = parse_tiles(first)
    for p in range(2, n_pages + 1):
        time.sleep(PACE)
        docs += parse_tiles(get(f'{base}?page={p}&order=cote%3Aasc'))
    seen, uniq = set(), []
    for d in docs:
        if d['url'] not in seen:
            seen.add(d['url'])
            uniq.append(d)
    return {'title': text(title.group(1)) if title else None,
            'pages': n_pages, 'docs': uniq}, n_pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int)
    ap.add_argument('--slug')
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    slugs = [args.slug] if args.slug else refresh_slugs()
    if args.limit:
        slugs = slugs[:args.limit]
    titles = dossier_titles()
    known = json.load(open(OUT)) if os.path.exists(OUT) else {}
    # the July file was {slug: [docs]}; lift it to the new shape
    known = {k: (v if isinstance(v, dict) else {'pages': None, 'docs': v}) for k, v in known.items()}
    n_pages = n_new = 0
    started = time.time()
    for i, slug in enumerate(slugs, 1):
        try:
            rec, fetched = harvest(slug, known, args.force)
        except Exception as e:                                # noqa: BLE001
            print(f'FAIL {slug}: {str(e)[:80]}', flush=True)
            continue
        rec['title'] = rec.get('title') or titles.get(slug) or known.get(slug, {}).get('title') or slug
        known[slug] = rec
        n_pages += fetched
        n_new += 1 if fetched else 0
        if i % 20 == 0 or i == len(slugs):
            json.dump(known, open(OUT, 'w'), ensure_ascii=False)
            print(f'  {i}/{len(slugs)} dossiers, {n_pages} pages fetched, '
                  f'{sum(len(v["docs"]) for v in known.values())} docs, '
                  f'{(time.time() - started) / 60:.0f} min', flush=True)
        time.sleep(PACE)
    json.dump(known, open(OUT, 'w'), ensure_ascii=False)
    docs = [d for v in known.values() for d in v['docs']]
    print(f'\n{len(known)} dossiers, {len(docs)} documents; '
          f'{sum(1 for d in docs if d.get("date"))} dated, '
          f'{sum(1 for d in docs if d.get("code"))} with a cote; '
          f'{n_new} dossiers (re)harvested this run')


if __name__ == '__main__':
    main()
