#!/usr/bin/env python3
"""Split bulk document catalogues into per-project JSON files the map UI
lazy-loads when a project sidebar opens (keeps projects_canada.geojson lean)."""
import collections
import gzip
import json
import os
import re
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'data', 'raw')
ARCHIVE_ONLY = '--archive-only' in sys.argv
NORMALIZE_ONLY = '--normalize-only' in sys.argv


OUR_ID_SUFFIX = re.compile(r'\s\((doc|BAPE) \d+\)$')


def attach_archive_urls():
    """Add archive_url beside url in every catalogue entry the R2 archive
    lane has mirrored (data/raw/archive_manifest.json.gz), and the document
    date the lane read off the registry's landing page (doc_date). Idempotent,
    and run before the hygiene pass so a date that has just arrived can take
    over from the registry-number suffix the pass used in its absence."""
    import glob
    parts = sorted(glob.glob(os.path.join(RAW, 'archive_manifest*.json.gz')))
    if not parts:
        return
    # The seed part (local uploads) and the lane part can both hold a URL;
    # take any field either one has rather than letting the later file win.
    man = {}
    for src in parts:
        for url, rec in json.load(gzip.open(src, 'rt')).items():
            cur = man.setdefault(url, {})
            for f in ('archive_url', 'doc_date', 'doc_ref'):
                if rec.get(f) and not cur.get(f):
                    cur[f] = rec[f]
    n_files = n_docs = n_dates = 0
    for path in glob.glob(os.path.join(ROOT, 'data', 'docs', '*', '*.json')):
        try:
            cat = json.load(open(path))
        except (ValueError, OSError):
            continue
        changed = False
        for d in cat.get('docs') or []:
            rec = man.get(d.get('url'))
            if not rec:
                continue
            au = rec.get('archive_url')
            if au and d.get('archive_url') != au:
                d['archive_url'] = au
                changed = True
                n_docs += 1
            dd = rec.get('doc_date')
            if dd and not d.get('date'):
                d['date'] = dd
                d['title'] = OUR_ID_SUFFIX.sub('', d.get('title') or '')
                changed = True
                n_dates += 1
        if changed:
            json.dump(cat, open(path, 'w'), ensure_ascii=False)
            n_files += 1
    print(f'archive links: {n_docs} added, {n_dates} document dates attached, '
          f'across {n_files} catalogues')


# ── Catalogue hygiene ─────────────────────────────────────────────────
# Registries list the same file under several tabs and phases, so the
# harvests filed it several times (Wheeler River: 464 entries, 207 files),
# and they title different files identically ("Appendix A", "Registration
# Document", "Public Notice"). Collapse repeats and give every remaining
# same-titled document a distinguishing suffix drawn from what the source
# publishes: a date when the copies differ by date (the UI shows it), else
# a meaningful filename, else the record's category, else the registry's
# own document number. Idempotent; runs last in every mode.
GENERIC_TITLE = re.compile(r'^(download|pdf|click here|view|link|file|document)\b', re.I)
FED_DOC = re.compile(r'/document/(\d+)$')
BAPE_DOC = re.compile(r'[?&]id=0*(\d+)')


def _norm_title(t):
    return ' '.join((t or '').split()).casefold()


def _stem(url):
    """Filename stem when it says something ("Limerock-EA-Pages-1-191"),
    None when it is an id, a hash or an API verb ("download")."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rstrip('/')
    base = path.rsplit('/', 1)[-1]
    base = re.sub(r'\.[A-Za-z0-9]{1,5}$', '', base)
    if not re.search(r'[A-Za-z]{3}', base) or re.fullmatch(r'[0-9a-f]{20,}', base, re.I):
        return None
    if base.casefold() in ('download', 'index', 'default', 'view', 'file', 'document'):
        return None
    return re.sub(r'[_\s]+', ' ', base).strip()


def _registry_id(url):
    m = FED_DOC.search(url)
    if m:
        return f'doc {m.group(1)}'
    m = BAPE_DOC.search(url)
    if m and 'bape' in url:
        return f'BAPE {m.group(1)}'
    return None


def _title_score(t):
    """Prefer the caption over the button text and the fuller title over
    the terser one ("Minister's Decision" beats "decision")."""
    t = (t or '').strip()
    return (0 if not t or GENERIC_TITLE.match(t) else 1, len(t))


def dedupe_docs(docs):
    """One record per URL. Copies are merged so a date, category or
    archive link recorded on any of them survives; the best title wins."""
    keep, order = {}, []
    for d in docs:
        u = (d.get('url') or '').strip()
        if u not in keep:
            keep[u] = dict(d)
            keep[u]['url'] = u
            order.append(u)
            continue
        k = keep[u]
        for f, v in d.items():
            if v in (None, ''):
                continue
            if f == 'title':
                if _title_score(v) > _title_score(k.get('title')):
                    k['title'] = v
            elif not k.get(f):
                k[f] = v
    return [keep[u] for u in order]


def _captured(d):
    c = str(d.get('captured') or '')
    return f'captured {c[:4]}-{c[4:6]}-{c[6:8]}' if re.fullmatch(r'\d{8}', c) else None


def _distinct(tags, title):
    """A tag set is usable only if every tag is present, no tag merely
    repeats the title, and the tagged titles come out distinct - compared
    the way the grouping compares them, so a re-run finds nothing to do."""
    if not all(tags):
        return False
    norm = [_norm_title(f'{title} ({t})') for t in tags]
    return len(set(norm)) == len(norm) and all(_norm_title(t) != _norm_title(title) for t in tags)


def disambiguate_titles(docs):
    """Suffix same-titled documents so each link reads distinctly."""
    groups = collections.defaultdict(list)
    for d in docs:
        groups[_norm_title(d.get('title'))].append(d)
    taken = set(groups)
    n = 0
    for key, ds in groups.items():
        if len(ds) < 2 or not key:
            continue
        # Copies that differ by date are already told apart by the UI.
        dated = collections.defaultdict(list)
        for d in ds:
            dated[d.get('date') or ''].append(d)
        for same_date in dated.values():
            if len(same_date) < 2:
                continue
            title = same_date[0]['title'].strip()
            candidates = (
                [d.get('code') for d in same_date],
                [_stem(d.get('url') or '') for d in same_date],
                [_captured(d) for d in same_date],
                [d.get('category') for d in same_date],
                [_registry_id(d.get('url') or '') for d in same_date],
            )
            for tags in candidates:
                if _distinct(tags, title) and not any(
                        _norm_title(f'{title} ({t})') in taken for t in tags):
                    break
            else:
                tags = [f'{i} of {len(same_date)}' for i in range(1, len(same_date) + 1)]
            for d, tag in zip(same_date, tags):
                d['title'] = f"{d['title'].strip()} ({tag})"
                taken.add(_norm_title(d['title']))
                n += 1
    return n


def normalize_catalogues():
    import glob
    n_files = n_dup = n_named = 0
    for path in glob.glob(os.path.join(ROOT, 'data', 'docs', '*', '*.json')):
        try:
            cat = json.load(open(path))
        except (ValueError, OSError):
            continue
        docs = cat.get('docs') or []
        before = json.dumps(docs, sort_keys=True)
        deduped = dedupe_docs(docs)
        n_dup += len(docs) - len(deduped)
        n_named += disambiguate_titles(deduped)
        if json.dumps(deduped, sort_keys=True) != before:
            cat['docs'] = deduped
            json.dump(cat, open(path, 'w'), ensure_ascii=False)
            n_files += 1
    print(f'catalogue hygiene: {n_dup} repeated records dropped, '
          f'{n_named} same-titled documents suffixed, {n_files} catalogues rewritten')


if NORMALIZE_ONLY:
    attach_archive_urls()
    normalize_catalogues()
    sys.exit(0)

if ARCHIVE_ONLY:
    attach_archive_urls()
    normalize_catalogues()
    sys.exit(0)

# ── BC EPIC ──────────────────────────────────────────────────────────
src = os.path.join(RAW, 'bc_doc_catalogue.json')
if os.path.exists(src):
    outdir = os.path.join(ROOT, 'data', 'docs', 'bc')
    os.makedirs(outdir, exist_ok=True)
    cat = json.load(open(src))
    n = 0
    for pid, entry in cat.items():
        docs = sorted(entry['docs'], key=lambda d: d.get('date') or '', reverse=True)
        json.dump({'project': entry['name'],
                   'docs': [{'title': d['title'] or d['file'], 'date': (d.get('date') or '')[:10],
                             'url': d['url']} for d in docs]},
                  open(os.path.join(outdir, f'{pid}.json'), 'w'), ensure_ascii=False)
        n += len(docs)
    print(f'bc: {len(cat)} files, {n} docs')

# ── Nova Scotia ──────────────────────────────────────────────────────
src = os.path.join(RAW, 'ns_doc_catalogue.json')
if os.path.exists(src):
    outdir = os.path.join(ROOT, 'data', 'docs', 'ns')
    os.makedirs(outdir, exist_ok=True)
    cat = json.load(open(src))
    n = 0
    for url, docs in cat.items():
        slug = url.rstrip('/').rsplit('/', 1)[-1].replace('.asp', '') or 'index'
        json.dump({'project': slug,
                   'docs': [{'title': d['title'], 'url': d['url']} for d in docs]},
                  open(os.path.join(outdir, f'{slug}.json'), 'w'), ensure_ascii=False)
        n += len(docs)
    print(f'ns: {len(cat)} files, {n} docs')

# ── Federal archive (pre-CEAA-2012 CEAR records) ─────────────────────
# These never had per-project catalogues: their documents only exist in the
# harvested search index as 'archive-document' rows carrying a relative_path
# into https://iaac-aeic.gc.ca/archives/evaluations.
import gzip

FED_ARCHIVE_BASE = 'https://iaac-aeic.gc.ca/archives/evaluations'
src = os.path.join(RAW, 'federal_list_all.json.gz')
if os.path.exists(src):
    outdir = os.path.join(ROOT, 'data', 'docs', 'federal')
    os.makedirs(outdir, exist_ok=True)
    by_project = {}
    for e in json.load(gzip.open(src, 'rt')):
        if e.get('document_type') != 'archive-document':
            continue
        pid, rel = e.get('project_id'), e.get('relative_path')
        if not pid or not rel:
            continue
        by_project.setdefault(pid, []).append({
            'title': ' '.join((e.get('document_title_en')
                               or e.get('file_name') or 'Document').split()),
            'category': e.get('document_category_en') or 'Archived',
            'url': FED_ARCHIVE_BASE + rel.replace('\\', '/'),
        })
    n = 0
    for pid, docs in by_project.items():
        seen, uniq = set(), []
        for d in docs:
            if d['url'] in seen:
                continue
            seen.add(d['url'])
            uniq.append(d)
        json.dump({'project': pid, 'docs': uniq},
                  open(os.path.join(outdir, f'{pid}.json'), 'w'), ensure_ascii=False)
        n += len(uniq)
    print(f'federal archive: {len(by_project)} files, {n} docs')


# ── Quebec BAPE ──────────────────────────────────────────────────────
# scripts/fetch_qc_docs.py harvests every dossier's documentation pages
# with each document's cote (RP1, DA3 ...) and date. File and project
# names keep the July 2026 rule (slug cut to 60 characters) so the R2
# archive keys derived from them stay put.
src = os.path.join(RAW, 'qc_bape_doc_catalogue.json')
if os.path.exists(src):
    outdir = os.path.join(ROOT, 'data', 'docs', 'qc')
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for slug, entry in json.load(open(src)).items():
        docs = entry['docs'] if isinstance(entry, dict) else entry
        if not docs:
            continue
        name = slug[:60]
        # keep whatever project label the catalogue already carries: the R2
        # archive keys were derived from it (July cut it at 64, files at 60)
        existing = os.path.join(outdir, f'{name}.json')
        project = name
        if os.path.exists(existing):
            try:
                project = json.load(open(existing)).get('project') or name
            except (ValueError, OSError):
                pass
        out = []
        for d in docs:
            rec = {'title': d.get('title') or 'Document', 'url': d['url']}
            if d.get('code'):
                rec['code'] = d['code']
            if d.get('date'):
                rec['date'] = d['date']
            out.append(rec)
        json.dump({'project': project,
                   'name': entry.get('title') if isinstance(entry, dict) else None,
                   'docs': out},
                  open(os.path.join(outdir, f'{name}.json'), 'w'), ensure_ascii=False)
        n += len(out)
    print(f'qc: {len(os.listdir(outdir))} files, {n} docs')

# ── Ontario provincial EA (ontario.ca project pages) ─────────────────
# The "Project documentation" sidebar on each project page links the notice
# of approval, ministry review and terms of reference -- these are the EA
# record, and they are the only documents Ontario publishes centrally.
src = os.path.join(RAW, 'on_ea_project_pages.json')
if os.path.exists(src):
    outdir = os.path.join(ROOT, 'data', 'docs', 'on')
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for rec in json.load(open(src)):
        docs = rec.get('docs') or []
        if not docs:
            continue
        json.dump({'project': rec.get('name') or rec['slug'], 'docs': docs},
                  open(os.path.join(outdir, f"{rec['slug']}.json"), 'w'),
                  ensure_ascii=False)
        n += len(docs)
    print(f'ontario provincial EA: {len(os.listdir(outdir))} files, {n} docs')

attach_archive_urls()
normalize_catalogues()
