#!/usr/bin/env python3
"""Mirror every document the atlas links to into our own R2 bucket.

Government and proponent hosts retire documents; this makes the copy we link
to permanent. The archive is keyed by the registry's own identifiers so the
key is predictable from the original URL, and a manifest
(data/raw/archive_manifest.json.gz) records what has been fetched, where it
landed, and what failed, so runs are resumable and idempotent.

  python3 scripts/archive_docs.py --budget 18000          # in the lane
  python3 scripts/archive_docs.py --only qc --limit 20     # spot run
  python3 scripts/archive_docs.py --dry-run --only federal # plan only

Credentials come from the environment (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE) and are handed to rclone
through its RCLONE_CONFIG_* variables; nothing is written to disk.

Federal links are landing pages, not files: the page carries an unquoted
href to /050/documents/p<proj>/<doc>.pdf when there is an attachment, and
about 44% are HTML-only notices, in which case the page itself is archived.
IAAC throttles by answering 404 for the whole host after a burst, so that
host is paced hard and canary-checked; see check_links.py.
"""
import argparse
import collections
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_links import CANARY, HostLimiter, SLOW_HOSTS, UA  # noqa: E402

MANIFEST = os.path.join(ROOT, 'data', 'raw', 'archive_manifest.json.gz')
MAX_BYTES = 600 * 1024 * 1024        # skip anything larger; flag for manual
MAX_FAILS = 4                        # give up on a URL after this many tries
IAAC = 'iaac-aeic.gc.ca'
FED_FILE_RX = re.compile(r'href=["\']?(/050/documents/[^"\'\s>]+)', re.I)
FED_DOC_RX = re.compile(r'/050/evaluations/document/(\d+)')
SAFE = re.compile(r'[^A-Za-z0-9._-]+')


def env(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f'{name} is not set')
    return v


def rclone_env():
    """rclone remote 'r2' defined purely through the environment."""
    e = dict(os.environ)
    e.update({
        'RCLONE_CONFIG_R2_TYPE': 's3',
        'RCLONE_CONFIG_R2_PROVIDER': 'Cloudflare',
        'RCLONE_CONFIG_R2_ACCESS_KEY_ID': env('R2_ACCESS_KEY_ID'),
        'RCLONE_CONFIG_R2_SECRET_ACCESS_KEY': env('R2_SECRET_ACCESS_KEY'),
        'RCLONE_CONFIG_R2_ENDPOINT':
            f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        'RCLONE_CONFIG_R2_NO_CHECK_BUCKET': 'true',
    })
    return e


def upload_dir(staging, dry_run=False):
    """Push the staging tree to the bucket; keys are the relative paths."""
    if dry_run or not any(os.scandir(staging)):
        return True
    cmd = ['rclone', 'copy', staging, f"r2:{env('R2_BUCKET')}",
           '--transfers', '8', '--s3-no-check-bucket', '--quiet']
    r = subprocess.run(cmd, env=rclone_env(), capture_output=True, text=True)
    if r.returncode != 0:
        print('rclone failed (exit', r.returncode, '):', flush=True)
        print(r.stderr.strip()[-2000:], flush=True)
        return False
    return True


# ── keys ──────────────────────────────────────────────────────────────
def safe(s, n=120):
    return SAFE.sub('_', urllib.parse.unquote(s)).strip('_')[:n] or 'file'


def key_for(jur, project, url, filename=None):
    """Deterministic object key. Mirrors the source's own ids where it has
    them; otherwise the URL's basename, or a hash when there is none."""
    path = urllib.parse.urlsplit(url).path
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    if jur == 'federal':
        m = FED_DOC_RX.search(url)
        base = filename or (f'{m.group(1)}.pdf' if m else os.path.basename(path))
    elif jur == 'bc':                        # /api/public/document/<id>/download
        m = re.search(r'/document/([0-9a-f]{24})', url)
        base = f'{m.group(1)}.pdf' if m else os.path.basename(path) or 'file'
    elif jur == 'qc':                        # voute.bape.gouv.qc.ca/dl/?id=<n>
        base = f"{q.get('id', ['file'])[0]}.pdf"
    else:
        base = os.path.basename(path)
        if not base or '.' not in base:
            base = hashlib.sha1(url.encode()).hexdigest()[:16] + '.html'
    return f'{jur}/{safe(str(project), 80)}/{safe(base)}'


# ── fetch ─────────────────────────────────────────────────────────────
def clean_url(url):
    """Some registries publish hrefs with spaces or line breaks inside them;
    urllib refuses those outright. Strip stray whitespace and encode the rest."""
    url = ''.join(url.split()) if '\n' in url or '\r' in url else url.strip()
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe='/%:@+,;=!$&\'()*')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path,
                                    parts.query.replace(' ', '%20'), ''))


def fetch(url, limiter, timeout=120):
    url = clean_url(url)
    host = urllib.parse.urlsplit(url).hostname or ''
    limiter.acquire(host)
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': UA, 'Accept': '*/*',
            'Accept-Language': 'en-CA,en;q=0.9,fr-CA;q=0.8'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip()
            clen = r.headers.get('Content-Length')
            if clen and int(clen) > MAX_BYTES:
                return None, ctype, f'too large ({int(clen) // 1_000_000} MB)'
            data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None, ctype, 'too large (streamed)'
            disp = r.headers.get('Content-Disposition') or ''
            return data, ctype, disp
    finally:
        limiter.release(host)


FED_DATE_RX = re.compile(r'<meta\s+name="Document Date"\s+content="(\d{4}-\d{2}-\d{2})', re.I)
FED_REF_RX = re.compile(r'Document reference number:\s*</strong>\s*([^<\s][^<]*?)\s*<', re.I)


def federal_meta(html):
    """The registry's document landing page carries the document's own
    date (<meta name="Document Date">) and its reference number. The
    catalogues have neither, and the date is what tells "Appendix A" from
    "Appendix A" - so keep both on the manifest record."""
    out = {}
    m = FED_DATE_RX.search(html)
    if m:
        out['doc_date'] = m.group(1)
    m = FED_REF_RX.search(html)
    if m:
        out['doc_ref'] = m.group(1).strip()[:40]
    return out


def resolve_federal(url, html):
    """Landing page -> (file url or None). None means HTML-only notice."""
    m = FED_FILE_RX.search(html)
    if not m:
        return None
    return urllib.parse.urljoin(url, m.group(1))


def ext_for(ctype, disp, fallback):
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp or '', re.I)
    if m:
        e = os.path.splitext(urllib.parse.unquote(m.group(1)))[1].lower()
        if e:
            return e
    return {'application/pdf': '.pdf', 'text/html': '.html',
            'application/msword': '.doc', 'application/zip': '.zip',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-excel': '.xls'}.get(ctype, fallback)


# ── work list ────────────────────────────────────────────────────────
def targets(only=None):
    out, seen = [], set()
    for path in sorted(glob.glob(os.path.join(ROOT, 'data', 'docs', '*', '*.json'))):
        jur = os.path.basename(os.path.dirname(path))
        if only and jur not in only:
            continue
        project = os.path.splitext(os.path.basename(path))[0]
        try:
            docs = json.load(open(path)).get('docs') or []
        except (ValueError, OSError):
            continue
        for d in docs:
            u = d.get('url')
            if not u or u in seen or not u.startswith(('http://', 'https://')):
                continue
            seen.add(u)
            out.append((u, jur, project, d.get('title') or ''))
            if d.get('fallback_url'):
                FALLBACK[u] = d['fallback_url']
    return out


FALLBACK = {}      # original url -> Wayback capture to use if the original is gone


def load_manifest(all_parts=True):
    """The lane writes archive_manifest.json.gz; local seeding writes
    archive_manifest_seed.json.gz so the two never conflict in git. Readers
    merge every part; only the lane's own part is rewritten here."""
    merged = {}
    parts = sorted(glob.glob(MANIFEST.replace('.json.gz', '*.json.gz'))) \
        if all_parts else [MANIFEST]
    for path in parts:
        if os.path.exists(path):
            merged.update(json.load(gzip.open(path, 'rt')))
    return merged


def save_manifest(m, path=MANIFEST):
    tmp = path + '.tmp'
    with gzip.open(tmp, 'wt') as f:
        json.dump(m, f, ensure_ascii=False)
    os.replace(tmp, path)


# ── bucket-side manifest checkpoint ──────────────────────────────────
# Git commits from the lane can fail (rebase conflicts on the binary manifest);
# the bucket copy under _meta/ is the checkpoint that survives that. Every
# flush uploads it; every run merges it in before deciding what to fetch.
BUCKET_MANIFEST_KEY = '_meta/archive_manifest.json.gz'


def rclone(*args):
    return subprocess.run(['rclone', *args], env=rclone_env(),
                          capture_output=True, text=True)


def push_manifest_to_bucket():
    r = rclone('copyto', MANIFEST, f"r2:{env('R2_BUCKET')}/{BUCKET_MANIFEST_KEY}",
               '--s3-no-check-bucket', '--quiet')
    if r.returncode != 0:
        print('manifest checkpoint upload failed:', r.stderr.strip()[-300:], flush=True)


def pull_manifest_from_bucket():
    """-> dict of records from the bucket checkpoint, or {} if none."""
    tmp = MANIFEST + '.bucket'
    r = rclone('copyto', f"r2:{env('R2_BUCKET')}/{BUCKET_MANIFEST_KEY}", tmp,
               '--s3-no-check-bucket', '--quiet')
    if r.returncode != 0 or not os.path.exists(tmp):
        return {}
    try:
        return json.load(gzip.open(tmp, 'rt'))
    except (OSError, ValueError):
        return {}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def merge_manifests(base, other):
    """Union; a record that names an uploaded object beats one that doesn't,
    otherwise the more recently fetched record wins."""
    for url, rec in other.items():
        cur = base.get(url)
        if cur is None:
            base[url] = rec
            continue
        if rec.get('key') and (not cur.get('key')
                               or (rec.get('fetched_at', '') > cur.get('fetched_at', ''))):
            base[url] = rec
        elif not rec.get('key') and not cur.get('key'):
            cur['fails'] = max(cur.get('fails', 0), rec.get('fails', 0))
        # document metadata is filled in by a later pass than the upload
        # (see --meta-backfill); it must survive whichever record wins
        for f in ('doc_date', 'doc_ref'):
            if not base[url].get(f) and (rec.get(f) or cur.get(f)):
                base[url][f] = rec.get(f) or cur.get(f)
    return base


def reconcile_from_bucket(manifest, public, only=None):
    """Rebuild manifest records for objects that exist in the bucket but
    are missing from the manifest (e.g. a run whose commit was lost). Keys
    are deterministic from the catalogue, so list the bucket and match."""
    r = rclone('lsjson', '-R', '--files-only', f"r2:{env('R2_BUCKET')}")
    if r.returncode != 0:
        print('bucket listing failed:', r.stderr.strip()[-300:], flush=True)
        return 0
    present = {e['Path']: e for e in json.loads(r.stdout)}
    print(f'bucket holds {len(present)} objects', flush=True)
    n = 0
    for url, jur, project, title in targets(only):
        if manifest.get(url, {}).get('key'):
            continue
        base = os.path.splitext(key_for(jur, project, url))[0]
        for ext in ('.pdf', '.html', '.docx', '.doc', '.zip', '.xlsx', '.xls', '.bin'):
            key = base + ext
            if key in present:
                manifest.setdefault(url, {'jur': jur, 'project': project}).update(
                    key=key, archive_url=f'{public}/{key}', bytes=present[key].get('Size'),
                    kind='html_notice' if ext == '.html' and jur == 'federal' else 'file',
                    title=title[:200], reconciled=True,
                    fetched_at=(present[key].get('ModTime') or '')[:19] + 'Z')
                n += 1
                break
    print(f'reconciled {n} records from the bucket', flush=True)
    return n


# ── main ─────────────────────────────────────────────────────────────
def meta_backfill(limit, budget):
    """Federal records archived before the lane kept the landing page's
    metadata: fetch the page again (HTML only, ~60 KB, same pacing and
    canary as the archive pass) and record doc_date / doc_ref. Needs no
    bucket access; the checkpoint upload happens when credentials exist."""
    manifest = load_manifest(all_parts=False)
    # Records the local seeder uploaded live in the seed part; date them
    # too, writing the result into the lane's part (same object, same key).
    every = load_manifest(all_parts=True)
    for u, r in every.items():
        if u not in manifest and r.get('jur') == 'federal' and r.get('key'):
            manifest[u] = dict(r)
    todo = [u for u, r in manifest.items()
            if r.get('jur') == 'federal' and r.get('key') and not r.get('doc_date')
            and not r.get('meta_fails', 0) >= MAX_FAILS
            and FED_DOC_RX.search(u)]
    todo.sort(key=lambda u: int(FED_DOC_RX.search(u).group(1)), reverse=True)
    todo = todo[:limit]
    print(f'{len(todo)} federal records to date (of '
          f'{sum(1 for r in manifest.values() if r.get("jur") == "federal" and r.get("key"))}'
          f' archived)', flush=True)
    if not todo:
        return
    limiter = HostLimiter(2, 0.6)
    try:
        fetch(CANARY[IAAC], limiter)
    except Exception as e:                                    # noqa: BLE001
        print(f'  canary FAIL {IAAC}: {e} -- not starting', flush=True)
        return
    started, n_ok, n_fail = time.time(), 0, 0
    can_push = bool(os.environ.get('R2_BUCKET'))
    for i, url in enumerate(todo, 1):
        if time.time() - started > budget:
            print('budget reached', flush=True)
            break
        rec = manifest[url]
        try:
            if i % 100 == 0:
                fetch(CANARY[IAAC], limiter)
            data, ctype, disp = fetch(url, limiter)
            if data is None or 'html' not in ctype:
                raise RuntimeError(disp or ctype)
            meta = federal_meta(data.decode('utf-8', 'replace'))
            if not meta.get('doc_date'):
                raise RuntimeError('no Document Date on page')
            rec.update(meta)
            rec.pop('meta_fails', None)
            n_ok += 1
        except urllib.error.HTTPError as e:
            n_fail += 1
            rec['meta_fails'] = rec.get('meta_fails', 0) + 1
            if e.code == 404:
                try:
                    fetch(CANARY[IAAC], limiter)
                except Exception:                              # noqa: BLE001
                    rec['meta_fails'] -= 1
                    print('  IAAC is refusing us -- stopping', flush=True)
                    break
        except Exception as e:                                # noqa: BLE001
            n_fail += 1
            rec['meta_fails'] = rec.get('meta_fails', 0) + 1
            rec['meta_error'] = str(e)[:120]
        if i % 100 == 0 or i == len(todo):
            save_manifest(manifest)
            if can_push:
                push_manifest_to_bucket()
            print(f'  {i}/{len(todo)}  dated {n_ok}, failed {n_fail}, '
                  f'{(time.time() - started) / 60:.0f} min', flush=True)
    save_manifest(manifest)
    if can_push:
        push_manifest_to_bucket()
    dated = sum(1 for r in manifest.values() if r.get('doc_date'))
    print(f'\nthis run: {n_ok} dated, {n_fail} failed; {dated} federal records carry a date')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--budget', type=int, default=3600, help='seconds')
    ap.add_argument('--only', help='comma-separated jurisdictions')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--flush-every', type=int, default=200)
    ap.add_argument('--reconcile', action='store_true',
                    help='rebuild manifest records from a bucket listing, then exit')
    ap.add_argument('--meta-backfill', type=int, metavar='N',
                    help='fetch document dates for up to N federal records archived '
                         'before the lane captured them, then exit')
    args = ap.parse_args()

    if args.meta_backfill:
        meta_backfill(args.meta_backfill, args.budget)
        return

    public = os.environ.get('R2_PUBLIC_BASE', '').rstrip('/')
    if not args.dry_run:
        env('R2_BUCKET')
        if not public:
            raise SystemExit('R2_PUBLIC_BASE is not set')

    manifest = load_manifest(all_parts=False)       # the lane's own part
    if not args.dry_run:
        from_bucket = pull_manifest_from_bucket()
        if from_bucket:
            before = sum(1 for v in manifest.values() if v.get('key'))
            merge_manifests(manifest, from_bucket)
            after = sum(1 for v in manifest.values() if v.get('key'))
            print(f'bucket checkpoint merged: {after - before} records recovered',
                  flush=True)
            save_manifest(manifest)
        # Always reconcile against the bucket before fetching: a run whose
        # commit was lost (it happened twice) is then recovered automatically
        # instead of being re-downloaded. Listing ~50k objects takes seconds.
        n = reconcile_from_bucket(manifest, public,
                                  set(args.only.split(',')) if args.only else None)
        if n:
            save_manifest(manifest)
            push_manifest_to_bucket()
        if args.reconcile:
            return
    known = load_manifest()                          # + seeded parts, for skipping
    todo = [t for t in targets(set(args.only.split(',')) if args.only else None)
            if not known.get(t[0], {}).get('key')
            and known.get(t[0], {}).get('fails', 0) < MAX_FAILS]
    # round-robin across hosts so the slow one doesn't serialise the run
    by_host = collections.defaultdict(collections.deque)
    for t in todo:
        by_host[urllib.parse.urlsplit(t[0]).hostname].append(t)
    order = []
    while any(by_host.values()):
        for h in list(by_host):
            if by_host[h]:
                order.append(by_host[h].popleft())
    todo = order[:args.limit] if args.limit else order
    done_before = sum(1 for v in known.values() if v.get('key'))
    print(f'{len(todo)} to fetch, {done_before} already archived, '
          f'{len(known) - done_before} pending/failed', flush=True)
    if args.dry_run:
        for u, jur, proj, _ in todo[:15]:
            print('  ', key_for(jur, proj, u), '<-', u[:90])
        return

    limiter = HostLimiter(2, 0.6)
    dead = set()
    for host in {urllib.parse.urlsplit(t[0]).hostname for t in todo}:
        if host in CANARY:
            try:
                fetch(CANARY[host], limiter)
            except Exception as e:                            # noqa: BLE001
                dead.add(host)
                print(f'  canary FAIL {host}: {e} -- skipping host', flush=True)

    staging = tempfile.mkdtemp(prefix='ea-archive-')
    started, n_ok, n_fail, n_bytes = time.time(), 0, 0, 0
    host_hits = collections.Counter()
    staged = []          # urls written to staging but not yet uploaded

    def flush():
        """Upload what is staged; only then does the manifest get to claim it."""
        if not staged:
            save_manifest(manifest)
            return True
        if upload_dir(staging):
            save_manifest(manifest)
            push_manifest_to_bucket()
            shutil.rmtree(staging)
            os.makedirs(staging)
            staged.clear()
            return True
        return False

    try:
        for url, jur, project, title in todo:
            if time.time() - started > args.budget:
                print('budget reached', flush=True)
                break
            host = urllib.parse.urlsplit(url).hostname
            if host in dead:
                continue
            rec = manifest.setdefault(url, {'jur': jur, 'project': project})
            rec['title'] = title[:200]
            try:
                # periodic canary on the throttling host
                host_hits[host] += 1
                if host == IAAC and host_hits[host] % 100 == 0:
                    fetch(CANARY[IAAC], limiter)

                try:
                    data, ctype, disp = fetch(url, limiter)
                except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                    if url not in FALLBACK:
                        raise
                    # the proponent removed it; the Internet Archive still has it
                    data, ctype, disp = fetch(FALLBACK[url], limiter)
                    rec['fetched_from'] = 'wayback'
                if data is None:
                    raise RuntimeError(disp)
                file_url, kind = url, 'file'
                if jur == 'federal' and 'html' in ctype:
                    page = data.decode('utf-8', 'replace')
                    rec.update(federal_meta(page))
                    fu = resolve_federal(url, page)
                    if fu:
                        data2, ctype2, disp2 = fetch(fu, limiter)
                        if data2 is None:
                            raise RuntimeError(disp2)
                        data, ctype, disp, file_url = data2, ctype2, disp2, fu
                    else:
                        kind = 'html_notice'
                if 'html' in ctype and jur not in ('federal', 'on', 'proponent', 'classea'):
                    # a registry answering HTML where a file was expected is
                    # a soft failure (login page, error page); don't archive it
                    raise RuntimeError(f'got html from {host}')
                ext = ext_for(ctype, disp, os.path.splitext(
                    urllib.parse.urlsplit(file_url).path)[1] or '.bin')
                key = key_for(jur, project, url)
                if not key.endswith(ext):
                    key = os.path.splitext(key)[0] + ext
                dest = os.path.join(staging, key)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                open(dest, 'wb').write(data)
                rec.update(key=key, archive_url=f'{public}/{key}',
                           bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                           content_type=ctype, kind=kind, file_url=file_url,
                           fetched_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
                rec.pop('error', None)
                n_ok += 1
                n_bytes += len(data)
                staged.append(url)
            except urllib.error.HTTPError as e:
                rec['fails'] = rec.get('fails', 0) + 1
                rec['error'] = f'HTTP {e.code}'
                n_fail += 1
                if host == IAAC and e.code == 404:
                    # could be a throttle, not a dead link: verify with the canary
                    try:
                        fetch(CANARY[IAAC], limiter)
                    except Exception:                          # noqa: BLE001
                        dead.add(host)
                        rec['fails'] -= 1          # not the URL's fault
                        print('  IAAC is refusing us -- dropping host for this run', flush=True)
            except Exception as e:                            # noqa: BLE001
                rec['fails'] = rec.get('fails', 0) + 1
                rec['error'] = str(e)[:160]
                n_fail += 1
            if staged and len(staged) % args.flush_every == 0:
                if not flush():
                    print('upload failing; stopping to avoid losing work', flush=True)
                    break
                print(f'  {n_ok} archived ({n_bytes / 1e9:.2f} GB), {n_fail} failed, '
                      f'{(time.time() - started) / 60:.0f} min', flush=True)
    finally:
        if not flush():
            # never let the manifest claim objects that aren't in the bucket
            print(f'FINAL UPLOAD FAILED -- {len(staged)} staged files will be '
                  f'refetched next run', flush=True)
            for url in staged:
                for k in ('key', 'archive_url', 'bytes', 'sha256',
                          'content_type', 'kind', 'file_url', 'fetched_at'):
                    manifest[url].pop(k, None)
            save_manifest(manifest)
        shutil.rmtree(staging, ignore_errors=True)

    total = sum(1 for v in manifest.values() if v.get('key'))
    gb = sum(v.get('bytes', 0) for v in manifest.values()) / 1e9
    print(f'\nthis run: {n_ok} archived, {n_fail} failed, {n_bytes / 1e9:.2f} GB')
    print(f'archive total: {total} objects, {gb:.1f} GB -> {MANIFEST}')


if __name__ == '__main__':
    main()
