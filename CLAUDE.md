# Canada EA Atlas + Mitigation Prediction Tool

Public name: "Canada EA Atlas" (REA map → "Canada Project Map" 2026-07-08 → "Canada EA Atlas" 2026-08-31; repo renamed ontario-rea-map → canada-ea-atlas).

National environmental-assessment map, document corpus, and an early-stage
mitigation prediction tool ("reverse-engineer an EA from a project description
and location"). Owner: towertonhunt. Built July 2026, designed to run and grow
unattended via scheduled GitHub Actions.

## Architecture in one paragraph
GitHub Actions workflows ("lanes", `.github/workflows/fetch-*.yml`) harvest
government registries — Actions runners do the scheduled fetching, committing
results to `main` (the Mac mini session DOES have internet as of 2026-09-02, so
one-off harvests and link audits can run locally; keep the lanes as the
durable path)
(the old working branch `claude/mac-mini-connection-ceehl5` was merged and deleted 2026-08-31). Local scripts integrate raw data into
`data/projects_canada.geojson` (the map) and `data/corpus/` (text for the
knowledge base). `index.html` is a static Leaflet map (GitHub Pages from
`main`, custom domain via CNAME) with per-project lazy-loaded document
sidebars (`docs_path` property -> `data/docs/<jur>/<id>.json`).

## Key commands
- Rebuild map: `python3 scripts/build_national_geojson.py`
- Split doc catalogues: `python3 scripts/split_doc_catalogues.py`
- Build corpus search index: `python3 scripts/build_corpus_search.py` (FTS5 -> data/corpus_search.sqlite3.gz for wiki.html)
- Extract conditions: `python3 scripts/extract_conditions.py` (BC; extend per source)
- Predict mitigations: `python3 scripts/mitigation_predict.py <archetype> [constraints...]`
- Validate all data: `python3 scripts/validate_data.py` (enums, geojson bbox, FTS DB, registers, known-dead URL shapes)
- Audit outbound links: `python3 scripts/check_links.py [--docs] [--sample N] [--only src]` -> data/link_health.json (soft-404 aware, per-host paced, canary-guarded; see Link lessons)
- Harvest Ontario provincial EA pages: `python3 scripts/fetch_on_ea_pages.py` -> data/raw/on_ea_project_pages.json (proponent/status/location/docs), then split_doc_catalogues + build_national_geojson
- Dedupe conditions: `python3 scripts/dedupe_conditions.py` (exact (project,text) dupes; idempotent)
- Baseline constraints (needs internet -> run in Actions): `scripts/baseline_query.py lat lon buffer_m`
- Routing engine: `scripts/routing/build_routes.py` per `routing/framework.json`

## Link lessons (2026-09-02 audit — user reported Detour Lake had no working EA links)
- Archived CEAR records (dashed ids like 10-03-52262, 144 projects) 404 on
  /050/evaluations/proj/<id>; the working URL is
  https://iaac-aeic.gc.ca/archives/evaluations + index relative_path (backslashes
  -> slashes). Their docs are `archive-document` rows in federal_list_all.json.gz
  (309 docs / 105 projects) -> data/docs/federal/<dashed-id>.json.
- iaac-aeic.gc.ca answers HTTP 200 + "We couldn't find that Web page" for
  numeric-only archived ids (soft 404), 404s without a browser User-Agent, and
  THROTTLES BY RETURNING 404 for the whole host (even its home page) after a
  burst — a block lasts ~1h+. check_links.py paces it at 1.2s and canary-checks
  the home page before trusting any 404 from it.
- Quebec REE renamed fiche.asp -> projet.asp (every one of 402 links was dead).
- NS list rows sometimes carry bare slugs ('harbour-hills-wind/') -> prefix
  https://novascotia.ca/nse/ea/. Catalogue keys differ on www./trailing slash;
  match on slug.
- Ontario AMIS persistent-linking URLs return a byte-identical JS shell for
  every id (incl. bogus ones): fine in a browser, unverifiable server-side ->
  reported as `js_app`, not ok/broken.
- ontario.ca EA project pages hold proponent, status, location, decision dates
  and a "Project documentation" sidebar (notice of approval / ministry review /
  ToR) — the only centrally published Ontario EA record. 481 docs / 176 projects.
- West Detour (2017) was a provincial ESR under the MNDM Class EA with federal
  designation declined -> in NO harvested registry. Same structural gap as
  Kakabeka/Matabitchuan; needs a Class-EA/proponent-ESR source. Tracked in
  known_projects_checklist.json (expect: tracked) so it keeps warning.

## Document archive on Cloudflare R2 (live 2026-09-04)
- Bucket `canadaeaatlas` (ENAM), public dev URL
  https://pub-3cf90e85dbe545f68d1fe908ecf95777.r2.dev (custom domain needs
  towerton.ca DNS on Cloudflare; it's at Infomaniak). Secrets on the repo:
  R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET /
  R2_PUBLIC_BASE. Token = Account API token, Object Read & Write, this bucket.
- Lane archive-r2.yml: every 6h, 5h budget, `scripts/archive_docs.py`.
  Keys mirror registry ids (federal/<pid>/<docid>.pdf, bc/<pid>/<docid>.pdf,
  qc/<dossier>/<bapeid>.pdf ...). Federal landing pages resolve to the PDF
  behind the unquoted `href=/050/documents/...`; HTML-only notices are kept
  as .html (kind=html_notice). rclone via RCLONE_CONFIG_R2_* env; do NOT set
  an ACL (R2 rejects it).
- Manifests: data/raw/archive_manifest.json.gz (lane) + archive_manifest_seed
  .json.gz (local seeding via scripts/seed_archive_local.py) — readers merge
  archive_manifest*.json.gz; split_doc_catalogues.py --archive-only attaches
  `archive_url` beside `url`; index.html shows "archived copy".
- Run 1 (2026-09-04): 10,936 objects / 24.7 GB in 5h, no IAAC throttle trip
  (~700 federal/h -> federal backfill ~2.5 days). Seed: 588 federal PDFs from
  archive/federal-workspace (+327 docs added to catalogues). Projection ~144 GB.
- Known failure classes: www.downloads.ene.gov.on.ca resets every connection
  (134 ON docs; dying host — Wayback fallback is the next idea); NS hrefs with
  whitespace (fixed via clean_url); BAPE returns HTML for a few ids.
- Not yet archived (need owner decision): Hydro One Class EA PDFs
  (ontario-classea/transmission, 319 files, all mapped to hydroone.com URLs,
  NOT in the map) and the West Detour ESR set in ~/Work/Detour Lake Mine.

## Proponent-site document discovery (started 2026-09-04)
- Why: registries only hold what regulators file; Class EA / ESR records live
  on proponent sites (the hand-built Hydro One harvest in ontario-classea/ was
  the model). User asked for a comprehensive proponent list first, then docs.
- `scripts/build_proponent_targets.py` -> data/raw/proponents/targets.json:
  aggregates proponents across the map + gap inventories, drops government
  bodies (GOV/LEAD_GOV regex, KEEP exceptions), merges ALIAS groups, adds a
  SEED list, ranks (projects*3 + docs*0.05 + $value/1e8 + seed bonus), looks
  up websites on Wikidata with a label-token check (an unchecked version
  matched CNRL -> CN Rail). Hand fixes: data/raw/proponents/overrides.json
  (website/name/aliases/skip). 4,784 targets; 47 of top 50 have sites.
- `scripts/proponent_discover.py` -> data/raw/proponents/sites/<key>.json:
  bounded polite crawl (robots, sitemap, 2s delay, 250 pages, depth 3) with a
  PRIORITY QUEUE on EA-specific terms (STRONG/WEAK regexes) + seed URLs; a
  plain BFS from the home page found only marketing PDFs on hydroone.com.
  Validation: 60 pages of hydroone.com (seeded at /major-projects) -> 763
  docs incl. 27 final ESRs, 18 draft ESRs, 46 NoCs (hand inventory had 325).
  Big proponents often use per-project microsites: feed gap-inventory project
  URLs as seeds (seed_urls in targets.json).
- Bot walls (2026-09-04): OPG/Suncor/BHP = Cloudflare Bot Management -> every
  headless variant 403s after the first page; `--headed` (real Chrome, local
  only) passes. Bruce Power rejects a stale UA; the default HeadlessChrome UA
  trips Cloudflare -> browser mode sends a current Chrome UA (check_links.UA).
  `scripts/proponent_wayback.py` -> data/raw/proponents/wayback/<key>.json:
  Internet Archive CDX index of every PDF/Office file ever seen on a host
  (opg.com 5,135 / suncor.com 8,353 / brucepower.com 1,110), zero load on the
  proponent, recovers removed files; one big query per host + backoff (CDX
  503s under paging). bhp.com is global -> needs a Canada filter.
  Full harvest 2026-09-05: 60 hosts, 151,701 files, 116,263 after NOISE filter,
  9,259 EA-typed (Manitoba Hydro 2,319; BHP 1,129; Hydro One 1,035; Enbridge
  984; NWMO 540; Suncor 431; Capital Power 402 incl. the Port Dover & Nanticoke
  REA + 4 amendments -- REA instruments the Access Environment parser misses).
- PROMOTION (2026-09-05, user: "proceed in parallel, spreadsheet to review"):
  `scripts/promote_proponent_docs.py` -> data/raw/proponent_docs_index.json
  (feature `source|name` -> docs; merged into that project's catalogue at the
  end of build_national_geojson, creating data/docs/proponent/<source>-<slug>
  .json when the project had none) + data/raw/proponent_libraries.json +
  data/docs/proponent/library-<key>.json (one "EA document library" feature
  per proponent: source `proponent_site`, jurisdiction 'Proponent website',
  no geometry). Rules: EA-typed or EA_SIGNAL in title/url; drop NOISE, media,
  non-Canada files on GLOBAL_HOSTS (bhp/equinor/sasol...); dedupe vs every
  catalogue; project match = distinctive name tokens (freq<=2 in that
  proponent's portfolio) as whole words in TITLE+FILENAME only (directory
  names gave ILM 1,209 false matches). 175,793 discovered -> 13,830 promoted
  (693 matched / 75 projects; 13,137 in 56 libraries). Docs carry
  `fallback_url` (Wayback capture); archive_docs.py uses it when the original
  fails and marks fetched_from=wayback. `--report x.xlsx` = review workbook.
- Lane discover-proponents.yml: weekly (Sun) + dispatch (top / refresh),
  budget 16000s, --browser: crawl -> Wayback harvest (90 min cap) -> promote ->
  rebuild -> commit (reset-to-main, no rebase).

## Document naming convention (2026-09-04/05, user: date + kind, distinct per link)
- Rule: every document link carries its issue date and a kind-specific name;
  identical titles in one sidebar are a defect. Ontario REA trigger case:
  Marsh Hill showed five "Renewable Energy Approval" links.
- REA instruments: `scripts/fetch_rea_instruments.py` reads page one of each
  Access Environment instrument PDF (+ proponent-hosted copies whose title/
  filename says REA) for heading + "Issue Date:" -> data/raw/rea_instruments
  .json; build_national_geojson.name_rea_documents() titles them
  "2013-Apr-15 - Renewable Energy Approval [Amendment]", oldest first, and
  suffixes " (proponent copy)" when a proponent file duplicates a registry
  instrument. Idempotent; re-run after any REA scrape.
- Catalogue hygiene: split_doc_catalogues.py ends every mode with
  attach_archive_urls() (archive_url + doc_date from the manifests, merged
  field-wise across parts) then normalize_catalogues(): one record per URL
  (fuller title wins), then same-titled/same-date docs get a suffix from, in
  order: BAPE cote, meaningful filename, Wayback capture date, category,
  registry id "(doc N)"/"(BAPE N)", "(2 of 3)". `--normalize-only` runs just
  that. validate_data.check_catalogues fails on repeated URLs. First pass
  dropped 14,117 repeated records (harvests filed files 2-4x; Wheeler River
  464 -> 207).
- Federal dates: IAAC landing pages carry <meta name="Document Date"> +
  "Document reference number"; archive_docs.py keeps doc_date/doc_ref on the
  manifest record while archiving, and `--meta-backfill N` re-reads pages of
  records archived earlier (archive-r2.yml runs 1,200/run after the archive
  pass). A date arriving replaces the "(doc N)" suffix.
- Quebec: `scripts/fetch_qc_docs.py` walks the dossier listing (29 pages)
  and EVERY documentation page of every dossier (25 tiles/page, ordered by
  cote) -> data/raw/qc_bape_doc_catalogue.json {slug: {title, pages, docs[
  title,url,code,date,author,...]}}; the July harvest had read page one only
  (7.3k of 62k docs). split_doc_catalogues writes data/docs/qc/<slug[:60]>
  .json keeping the existing `project` label (R2 keys depend on it).
  `scripts/link_qc_bape.py` matches REE projects to dossiers by distinctive
  name tokens (>=3 shared, or 2 covering >=75% of both) -> data/raw/
  qc_bape_links.json; hand fixes in qc_bape_overrides.json; the builder sets
  docs_path/doc_count/bape_url on the REE feature (REE itself publishes no
  documents). Sidebar shows "CODE · title (date)". Lane fetch-qc-docs.yml:
  weekly (Wed) + dispatch, resumable by page count.

## Data inventory (as of 2026-07-05)
- Map: 18,401 features. Federal 6,576 (complete registry incl. federal-lands
  + archived), BC 358, QC 402, NS 248, NL 1,508, MB 2,716, ON REA 197,
  ON provincial EA 189, ON abandoned mines (AMIS) 6,207 (opt-in layer).
- Documents linked (data/docs/): federal 44,230 / BC 17,628 / QC (BAPE) 7,359 /
  NS 2,199 / ON REA in projects.geojson. ~73k total.
- Corpus (data/corpus/): bc/ 291 docs ~3.8M words; federal/ + ontario/ filling
  via lanes fetch-fed-decisions + fetch-on-conditions (576 decision statements
  + 580 condition docs federal; REA approval PDFs + 774 ERO notices Ontario).
- Conditions (data/conditions/): bc_conditions.json.gz — 1,884 records,
  heuristic classification (~35% 'other' — needs LM refinement pass).
- Routing: routing/framework.json (Dillon Wawa-Timmins study transcribed),
  routing/layers/ (12 LIO clips), routing/results/ v0 validated within 10%
  of the professional study (see validation_notes.md).

## Scheduled lanes (server-side, run from main's workflow copies)
- fetch-ea-data.yml: every 2h — federal index crawl (COMPLETE; now freshness).
  Crawl capped by Azure search 100k-skip: comments tail unreachable, accepted.
- fetch-north-geo.yml: every 6h — territories probes + GeoNames gazetteer
  (BROKEN: never commits data/geo/ca_places.json — debug unzip/csv step).
- archive-wayback.yml: 4x daily — submits all known document URLs to
  web.archive.org/save (progress: data/raw/wayback_done.json.gz; full
  sweep of ~73k URLs takes ~2.5 months at SPN-safe pace, then freshness).
- watch-new-filings.yml: daily — diffs newest federal registry entries
  (sort=PostedDateDesc) vs federal_list_all.json.gz, appends new ones,
  waybacks new doc pages immediately, queues them in
  data/raw/new_filings_queue.json, and mirrors files to Drive
  (gdrive:Canada-EA-Archive/) IF the repo secret RCLONE_DRIVE_TOKEN is
  set — until then the queue just accumulates, nothing is lost.
  PROVINCIAL pass added 2026-07-08 (PR #4): BC EPIC newest documents
  (direct download URLs -> Drive-mirrorable), QC REE dossier diff by
  update date, NS/MB/NL project-page diff; state in
  data/raw/watch_state.json, queue in new_filings_prov_queue.json.
  Still open: proponent-site adapters (wayback-on-discovery is the
  fallback), ERO/Ontario notice diffing, BAPE per-project doc diffing.
- fetch-on-permits.yml: daily — AMIS/compliance/assurance + active-mines recon.
- fetch-gap-reconcile.yml: weekly (Mon) — harvests NRCan Major Projects
  Inventory (score-10 CKAN dataset; table is XLSX, FR-first, data on a
  later sheet -> rows_from_xlsx scans sheets for the Nom du projet header),
  normalizes to the common inventory schema (data/raw/gap_inventories/
  *.json), runs scripts/gap_reconcile.py -> data/gap_report.json. WORKING
  2026-07-09: 474 majors, 318 geocoded -> 124 gap / 175 weak / 175 matched.
  IDF-weighted name coverage (distinctive tokens drive matches, common ones
  don't) + proponent + geo proximity; verdicts matched/weak/gap, gaps
  sorted by value. Top gaps to triage: Jansen (BHP potash), Syncrude
  Mildred Lake Ext., NICO, Ekati Misery Deep, plus new-type AI data-centre
  parks. Some 'gaps' are findability (in our federal data under other
  names) -> that's the review queue. _raw/nrcan_catalogue.json lists other
  NRCan datasets (Economic Points CSV w/ coords, by-municipality, Northern
  Major Projects, Inventory of Major Alberta Projects) to add as extra
  *.json inventories. validate_data.py WARNs high-value (>=$1B) gaps daily.
  UPDATE 2026-07-10: 3 inventories live (MPI 474 + Economic Points 998 +
  AB majors 138 after EA-relevance filter; by-municipality REJECTED —
  aggregate stats, no names). 'matched' now requires name-only score >=
  0.5 (NAME_FLOOR; boosts corroborate, don't establish — Ranney/Healey
  bug); load_ours() excludes nrcan_gap pins (feedback loop);
  data/gap_overrides.json = human-confirmed verdicts (review-queue
  output). Overlay 374 pins. REGRESSION GUARD:
  data/known_projects_checklist.json (13 named majors incl. user-reported
  OPG/Atura set) checked daily by validate_data.py with expectation
  ladder present>visible>tracked. STILL-OPEN structural gap: Ontario
  Class-EA-tier projects (Kakabeka, Matabitchuan GS redevs; Atura Halton
  Hills GS, Niagara Hydrogen) are in NO harvested source -> durable fix
  is an ERO notices lane (ero.ontario.ca project notices under EAA) +
  proponent aliasing (Napanee GS listed under builder TransCanada).
NOTE: everything now lives on main (branch consolidation 2026-08-31); commit lane fixes directly to main.

## App build (website + iPhone)
docs/APP_PLAN.md is the plan. Fable design tasks F1-F4 DONE 2026-07-10 ->
docs/design/ (IA, process model + data/process_frameworks.json, design
system + tokens + mockup.html, commitments UX).
BUILT 2026-07-10: scripts/build_api.py -> data/api/ (18.8k-row index w/
per-project process state per F3 mappings; 156 deep-tier detail files) +
search.html (S2) + project.html (S3 w/ process rail, docs, commitments,
predictor bridge) — verified headless-Chromium, linked from map header,
api check in validate_data.py. REBUILD ORDER: build_national_geojson ->
gap_reconcile -> build_api (api row count must match geojson).
Data fixes live: inventory enrichment (scripts/enrich_from_inventories.py,
strict bidirectional matching; Waasigan/Hydro One case) + 'Unmatched majors'
opt-in map layer from gap_report.

## Content pipeline (substack -> YouTube)
Owner's substack = 'Unceded Futures' (towerton.substack.com): settler
EA-veteran, one project/month, arc = scene -> history -> legal turning
point (Calder->Yahey) -> Bill C-5/Bill 5 bridge. 7 posts mirrored at
data/raw/substack/posts/ (owner-provided export; Substack Cloudflare
403s ALL datacenter fetches incl. runners — don't retry scraping).
docs/CONTENT_PIPELINE.md = strategy: every announced post subject
verified present in our DB (Prosperity x3 records, Site C deep-tier,
Ring of Fire regional assessment, Grassy Narrows Mercury Care Home
record). Formats: companion dossier / promise audit / invisible
projects / filings watch. NEXT: subjects.json post->project-id links;
indigenous_rights_tluse pre-filtered commitments view; Mackenzie Gas
Project geocode. PENDING FROM OWNER: Rod Northey EA-book digitization
(on his mac mini) -> project list as gap inventory (verification) +
excerpts to corpus/project pages (keep full text PRIVATE — published
work; facts/quotes only in public repo).

## Newest lanes (2026-07-10)
- fetch-ero-notices.yml: daily — ERO (ero.ontario.ca) EA/project-notice
  crawl; keyword queries + Instrument-type sweep (ero_notice_type[d]),
  resumable state data/raw/ero/ero_state.json; keeps -> gap inventory
  ero_on.json. Run #2: 56 EA notices; instrument sweep deepens daily —
  watch for Kakabeka/Matabitchuan/Atura appearing (checklist WARNs flip).
- fetch-substack.yml: weekly mirror attempt (blocked; owner export is
  the real source — lane kept for wayback-mediated retries only).

## Project footprints & layouts + aerial basemap (2026-09-05)
- User ask: zoomed-in projects should show the layout (turbines, pits, dams,
  lines), not a pin, on Google-Earth-class imagery.
- Basemap: index.html now has Map / Aerial / Auto (Esri World Imagery, keyless,
  native z19 ~0.3-0.5 m in settled Canada, upscaled to z21, with Esri
  transportation + places reference labels). Auto = light map, flips to
  imagery at IMAGERY_ZOOM 12. Choice persists in localStorage.
- Footprint model: data/footprints/<pid>.json (pid = make_id(jur,name), the
  API's stable id; make_id now lives in scripts/footprints_common.py and
  build_api imports it) = FeatureCollection of design elements with `role`
  (ROLES in footprints_common; FOOTPRINT_STYLES in index.html must stay in
  sync), `source`, provenance. data/footprints/index.json summarises; build_
  national_geojson attaches footprint_path/kind/n/roles/bbox/sources/
  confidence; validate_data check_footprints() enforces index<->file<->geojson.
- index.html lazy-loads footprints in view from FOOTPRINT_ZOOM 11 (selected
  project from 9), draws turbine glyphs / shape markers / polygons / lines,
  adds a "Project layout" legend + sidebar block (element counts, source,
  notes, zoom-to), and hoists map-like documents into "Maps, figures & site
  plans" (MAP_DOC_RX mirrors scripts/find_map_documents.py).
- SOURCE 1 (offline, live): scripts/extract_rea_layouts.py -- every Ontario
  REA instrument carries a schedule of equipment UTM coordinates (turbines,
  inverters, transformers, substations + sound power + model). Parser glues
  PDF-wrapped numbers, ids on the line above, E/N in either order (value
  ranges decide), zone from "Z17-NAD83"/"UTM17" else inferred vs the pin,
  drops rows >35 km from the pin. 105/222 corpus docs -> 61 projects, 1,775
  elements (550 turbines, 520 inverters, 277 transformers, 75 substations).
  Found > num_turbines = alternates the approval permits (noted, not an error).
- CORPUS FILL (2026-09-06, user: Zephyr Brooke-Alvinston had no layout): only
  222 of 555 Access Environment instrument PDFs were in the corpus; 124 of
  135 layout-less REA projects simply had no text. scripts/fetch_rea_corpus.py
  (lane step, needs poppler + tesseract on the runner) fetches approvals AND
  amendments, pdftotext -layout, OCRs image pages (Armow / Grand Renewable
  wind / Amherstburg schedules are scanned tables pypdf returns empty),
  --refresh-missing re-does corpus docs that mention equipment coordinates
  but hold no rows. Zone rule: a stated zone wins (Grey Highlands' registry
  pin was 403 km off; rows are anchored to their own median). build_national_
  geojson re-pins any feature whose point is >2 km from the layout centroid
  (geocode='layout', geocode_original kept): 27 REA pins moved, Goshen 14 km.
- SOURCE 2 (network, lane fetch-footprints.yml weekly Sun + dispatch):
  scripts/build_footprints.py --source gis|cwtd|osm, staging in
  data/raw/footprints/<src>/, --merge-only recombines (REA > gis > cwtd > osm;
  cross-source point dedupe 8 m, turbines 60 m; a source's own rows never
  collapse). gis = IAAC "Supporting geospatial data" attachments (75 docs on
  federal_iaac landing pages -> href=/050/documents zip; pure-python KML +
  shapefile readers, UTM .prj native, other CRS via pyproj) + any KML/KMZ/SHP
  link (find_map_documents gis_queue). cwtd = Canadian Wind Turbine Database
  (CKAN discovery + ftp.maps.canada.ca fallback; matched by distinctive name
  tokens + <=30 km, proximity-only <=4 km). osm = Overpass around the pin
  (radius by sector), roles from tags; unnamed elements kept only if they fit
  the sector and sit within radius/2; line-only proximity results dropped
  (the grid passing by). UNTESTED AGAINST LIVE SERVICES (sandbox has no
  egress): first lane run will show which CKAN/Overpass assumptions hold.
- Not done / next: georeferencing figure PDFs (site plans, GA drawings) --
  find_map_documents.py already indexes 2,556 map-like docs / 300 projects as
  the queue; BC EPIC and Quebec have no GIS attachments in catalogues;
  Alberta AER / BC EAO spatial datasets (project boundary polygons) would be
  the next bulk footprint source; project.html has no map yet.

## Known open threads (priority order)
1. Corpus quality: BC DONE 2026-07-06 -> bc_conditions_v2.json.gz
   (741 real measures kept of 1,884 v1 records; 1,242 were OCR/boilerplate
   noise — v1 was inflated. discipline 'other' 35% -> 18.8%; 0 enum
   violations; multi-measure splits use -a/-b ids). Merge tool:
   scripts/merge_reclassified_conditions.py. FEDERAL DONE 2026-07-06 ->
   federal_conditions_v2.json.gz: 4,946 clean conditions from 110 decision
   statements (8,272 extracted; 216 non-obligation discards; 3,110 dupes
   from repeated/annual-report text removed by (project_id, normalized
   text) dedup). Pipeline: scripts/extract_federal_conditions.py ->
   34 shards in data/conditions/shards_federal/ -> LM agents classify
   (prompts carry hint-override rules: health-section dust/noise ->
   air_quality/noise_vibration, SAR species list, ARD/tailings ->
   waste_hazmat, etc.) -> scripts/merge_federal_conditions.py (positional
   join; condition_ids repeat in source, never join on them). NEXT:
   Ontario REA conditions same pass (ontario_conditions.json.gz is v1),
   ONTARIO DONE 2026-07-07 ->
   ontario_conditions_v2.json.gz: 1,037 clean REA conditions of 2,319 v1
   records (508 ERT hearing-notice/header/definition discards, 774
   template dupes). Same shard pipeline: shards_ontario/ + 
   scripts/merge_ontario_conditions.py. DONE 2026-07-07: all three v2
   sets wired into scripts/mitigation_predict.py (Ontario re-merged with
   project-name keying -> 1,627; pool 7,265). Predictor matches
   primary+secondary disciplines, reports timing + jurisdiction mix;
   --full pre-generates data/predictions/<archetype>_register.json.
   INTEGRITY 2026-07-08: scripts/validate_data.py added; found + fixed
   49 exact-duplicate BC conditions (741->692; BC merge never text-
   deduped, unlike fed/on) via scripts/dedupe_conditions.py, and one
   US-geocoded federal point (Salt Lake City, lat 40.83) that slipped
   the map bbox (lower bound 40 -> 41.5 in build_national_geojson.py).
2. Baseline engine: VALIDATED 2026-07-06 — Adelaide demo returns 4
   constraint HITs (wetland, waterbody, watercourse, aggregate). Root
   causes were (a) LIO ignoring distance/units -> client-side envelope;
   (b) GeoHub catalogue mixing WMS endpoints into `rest` -> normalize
   /services/ to /rest/services/ and strip WMSServer suffix.
3. Gazetteer: FIXED 2026-07-05 (cd /tmp bug) — data/geo/ca_places.json
   committed; build script geocodes MB/NL/NS/ON-prov by municipality field
   then name n-grams (1,871 pins, flagged geocode=approximate).
4. Routing v1: add DEM terrain (12% weight), CLUPA/mining claims/railways
   (DCAT title mismatches), finer grid, corridor export; demo overlay page.
5. Demo UI: DONE 2026-07-07 -> predict.html (archetype + constraint
   checkboxes -> filtered register from data/predictions/; linked from
   map header). Map-click -> live baseline_query constraints still open.
6. Wiki: DONE 2026-07-08 -> wiki.html. FTS5 index (594 docs) built by
   scripts/build_corpus_search.py -> data/corpus_search.sqlite3.gz
   (15MB gz), loaded in-browser via OFFICIAL @sqlite.org/sqlite-wasm
   (vendor/sqlite3.mjs+wasm) using sqlite3_deserialize. NOTE: stock
   sql.js has NO fts5 - must use the sqlite.org build. Ranked bm25 +
   snippet(); raw-query-then-tokenized fallback for bad FTS syntax.
   Future: sql.js-httpvfs range requests to avoid the 15MB upfront load.
7. Active mines layer: NRCan 900A / OGSEarth (recon files in data/raw).
8. SK/NB/PE parsing — probes analyzed 2026-07-07, all need refetch:
   PEI pei_ea_list.html is a JS-rendered shell (0 links; find the ajax
   endpoint in page source); NB registrations URL 404s (find current EIA
   registrations path on www2.gnb.ca); SK envrbrportal is a Dynamics
   portal — data loads client-side, try the portal's OData/API endpoints
   (Dynamics portals expose /_odata or /_api) before scraping.

## Hard-won environment lessons
- Government sites 404/500 GitHub runners with generic UAs: ALWAYS use a full
  Chrome UA string for gc.ca/ontario.ca; retry loops mandatory.
- IAAC exploration API: POST search='%2A' (pre-encoded), sort='BestMatchDesc',
  cookies + Referer + X-Requested-With required; responses double-JSON-encoded.
- Actions runners get evicted ~60min: checkpoint + resume (see fetch-ea-data).
- GitHub rejects files >100MB: slim + gzip with mtime=0 (deterministic).
- Quebec REE has no document links; documents live at BAPE (voute dl/?id=).
- ArcGIS Hub portals expose DCAT at /api/feed/dcat-us/1.1.json — always try
  that before scraping HTML.
- Session containers restart often, killing crons/waiters: re-arm on wake;
  scheduled Actions on main are the only truly durable automation.
