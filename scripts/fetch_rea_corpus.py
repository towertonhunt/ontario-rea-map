#!/usr/bin/env python3
"""Fill the Ontario REA corpus: every Access Environment instrument PDF
(approval + amendments) linked from an ontario_rea map feature -> text in
data/corpus/ontario/<doc_id>.txt.gz + index.json (same layout the
fetch-on-conditions lane wrote, so extract_rea_layouts.py sees them).

Why: the equipment-coordinate schedules that give us turbine / inverter /
substation layouts live in these PDFs, and amendments carry revised
locations. The old lane stopped at 222 of ~600 instruments.

Text extraction: `pdftotext -layout` (poppler) when available, else pypdf.
Pages that come back (near) empty are image scans -- the schedule tables in
several approvals (Armow, Grand Renewable wind, Amherstburg) are exactly
that -- so those pages are OCR'd with tesseract when installed
(pdftoppm -r 300 -> tesseract --psm 6). `--refresh-missing` re-extracts
existing corpus docs that mention "Coordinates of the Equipment" but hold no
coordinate rows.

NEEDS INTERNET (lane fetch-footprints.yml). Usage:
  fetch_rea_corpus.py [--budget SEC] [--limit N] [--refresh-missing] [--dry-run]
"""
import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from footprints_common import ROOT  # noqa: E402

CORPUS = os.path.join(ROOT, 'data', 'corpus', 'ontario')
INDEX = os.path.join(CORPUS, 'index.json')
GEOJSON = os.path.join(ROOT, 'data', 'projects_canada.geojson')
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
COORD_HINT = re.compile(r'coordinates of the equipment|easting|northing', re.I)
COORD_ROW = re.compile(r'\b[2-7]\d{2}[, ]?\d{3}(?:\.\d+)?\s+[4-6][, ]?\d{3}[, ]?\d{3}\b')


def log(*a):
    print(*a, flush=True)


def doc_id_for(url):
    return 'rea-' + re.sub(r'[^A-Za-z0-9]+', '', url.rsplit('/', 1)[-1])[:40]


def get(url, tries=3):
    last = None
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/pdf,*/*'})
            return urllib.request.urlopen(req, timeout=120).read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (t + 1))
    raise last


def have(cmd):
    return shutil.which(cmd) is not None


def pdf_pages_text(pdf_bytes):
    """-> list of page texts. poppler first (keeps table columns on one
    line), pypdf as the fallback."""
    if have('pdftotext'):
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as fh:
            fh.write(pdf_bytes)
            path = fh.name
        try:
            out = subprocess.run(['pdftotext', '-layout', path, '-'], capture_output=True, timeout=180)
            txt = out.stdout.decode('utf-8', 'replace')
            pages = txt.split('\f')
            if pages and not pages[-1].strip():
                pages = pages[:-1]
            if any(p.strip() for p in pages):
                return pages
        except Exception as e:  # noqa: BLE001
            log('  pdftotext failed:', str(e)[:60])
        finally:
            os.unlink(path)
    try:
        from pypdf import PdfReader
        return [(pg.extract_text() or '') for pg in PdfReader(io.BytesIO(pdf_bytes)).pages]
    except Exception as e:  # noqa: BLE001
        log('  pypdf failed:', str(e)[:60])
        return []


def ocr_pages(pdf_bytes, page_numbers):
    """OCR selected 1-based pages -> {page: text}. Needs pdftoppm + tesseract."""
    if not (have('pdftoppm') and have('tesseract')) or not page_numbers:
        return {}
    out = {}
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'doc.pdf')
        open(path, 'wb').write(pdf_bytes)
        for n in page_numbers:
            try:
                subprocess.run(['pdftoppm', '-r', '300', '-f', str(n), '-l', str(n), '-gray', '-png',
                                path, os.path.join(td, f'p{n}')], capture_output=True, timeout=120, check=True)
                png = next((os.path.join(td, x) for x in os.listdir(td) if x.startswith(f'p{n}') and x.endswith('.png')), None)
                if not png:
                    continue
                r = subprocess.run(['tesseract', png, '-', '--psm', '6'], capture_output=True, timeout=180)
                out[n] = r.stdout.decode('utf-8', 'replace')
                os.unlink(png)
            except Exception as e:  # noqa: BLE001
                log(f'  ocr page {n} failed:', str(e)[:60])
    return out


def extract(pdf_bytes):
    """Full text with OCR filled in for image pages. Returns (text, n_ocr)."""
    pages = pdf_pages_text(pdf_bytes)
    if not pages:
        return '', 0
    doc_has_hint = any(COORD_HINT.search(p) for p in pages)
    # image pages: almost no text; also pages that carry the schedule header
    # but no coordinate rows (table drawn as an image below a text header)
    need = []
    for i, p in enumerate(pages, 1):
        stripped = re.sub(r'\s+', ' ', p).strip()
        if len(stripped) < 80:
            need.append(i)
        elif doc_has_hint and re.search(r'schedule\s+[a-c]\b|table\s+b1|coordinates', p, re.I) \
                and not COORD_ROW.search(p) and len(stripped) < 1200:
            need.append(i)
    ocr = ocr_pages(pdf_bytes, need[:40]) if need else {}
    for n, txt in ocr.items():
        if len(txt.strip()) > len(pages[n - 1].strip()):
            pages[n - 1] = pages[n - 1] + '\n' + txt
    return '\n'.join(pages), len(ocr)


def save(doc_id, text):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
        gz.write(text.encode())
    open(os.path.join(CORPUS, f'{doc_id}.txt.gz'), 'wb').write(buf.getvalue())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--budget', type=int, default=1500)
    ap.add_argument('--limit', type=int, default=100000)
    ap.add_argument('--refresh-missing', action='store_true',
                    help='re-extract existing docs that mention equipment coordinates but hold no rows')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    os.makedirs(CORPUS, exist_ok=True)
    index = json.load(open(INDEX)) if os.path.exists(INDEX) else []
    by_id = {e['doc_id']: e for e in index}
    log(f'tools: pdftotext={have("pdftotext")} pdftoppm={have("pdftoppm")} tesseract={have("tesseract")}; '
        f'{len(index)} docs in corpus')

    queue = []   # (url, project, title, existing_entry|None)
    geo = json.load(open(GEOJSON))
    for f in geo['features']:
        p = f['properties']
        if p.get('source') != 'ontario_rea':
            continue
        for s in p.get('doc_sections') or []:
            for d in s.get('docs') or []:
                u = d.get('url') or ''
                if 'accessenvironment' not in u or not u.lower().endswith('.pdf'):
                    continue
                did = doc_id_for(u)
                ex = by_id.get(did)
                if ex is None:
                    queue.append((u, p['name'], d.get('title'), None))
                elif a.refresh_missing:
                    path = os.path.join(CORPUS, f'{did}.txt.gz')
                    try:
                        t = gzip.open(path, 'rt', errors='replace').read()
                    except OSError:
                        t = ''
                    if COORD_HINT.search(t) and not COORD_ROW.search(t):
                        queue.append((u, p['name'], d.get('title'), ex))
    seen = set()
    queue = [q for q in queue if not (q[0] in seen or seen.add(q[0]))]
    log(f'{len(queue)} instrument PDFs to fetch ({sum(1 for q in queue if q[3]) } refreshes)')
    if a.dry_run:
        for q in queue[:20]:
            log('  ', q[1][:40], '|', q[2], '|', q[0])
        return

    t0, done, ok, n_ocr_docs = time.time(), 0, 0, 0
    for url, project, title, ex in queue:
        if done >= a.limit or time.time() - t0 > a.budget:
            break
        done += 1
        did = doc_id_for(url)
        try:
            blob = get(url)
            if not blob.startswith(b'%PDF'):
                log('  not a PDF:', url[-40:], blob[:40])
                continue
            text, n_ocr = extract(blob)
            if len(text.strip()) < 300:
                log('  empty text:', url[-40:])
                continue
            save(did, text)
            rows = len(COORD_ROW.findall(text))
            meta = {'jurisdiction': 'ontario_rea', 'project': project, 'title': title, 'url': url,
                    'doc_id': did, 'chars': len(text)}
            if n_ocr:
                meta['ocr_pages'] = n_ocr
                n_ocr_docs += 1
            if ex is not None:
                ex.update(meta)
            else:
                index.append(meta)
                by_id[did] = meta
            ok += 1
            log(f'  {ok:4d} {project[:40]:40} {title or "":34} rows={rows:3d} ocr={n_ocr}')
        except Exception as e:  # noqa: BLE001
            log('  FAIL', url[-40:], str(e)[:80])
        if ok % 20 == 0:
            json.dump(index, open(INDEX, 'w'), ensure_ascii=False)
        time.sleep(0.8)
    json.dump(index, open(INDEX, 'w'), ensure_ascii=False)
    log(f'DONE: {ok} extracted ({n_ocr_docs} with OCR), {len(queue) - done} left in queue, '
        f'{len(index)} docs in corpus')


if __name__ == '__main__':
    main()
