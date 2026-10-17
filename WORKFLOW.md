# WORKFLOW — in-depth crawl → verify → automate → push, end to end

Every command here was executed for real on a 2-core / 16GB Azure spot VM with a 250GB `/data` disk. Cross-references to the seven live repos (`cloudflare`, `copilotkit`, `sarvam-docs`, `ai-docs`, `google-cloud-docs`, `comfy-docs`, `fish-audio-docs`) are marked 📎.

---

## 1. WHICH STACK, AND WHY (the fetch ladder)

Four libraries, used **in a strict fallback ladder**. Never skip levels. Never run everything headless.

| Level | Tool | Job | Speed | When it wins |
|---|---|---|---|---|
| 1 | **Scrapling** `Fetchers.requests.Fetcher` | HTTP GET with stealth headers | ~0.2–1s/page | 90–95% of static doc pages |
| 2 | **ScrapeGraphAI** `convert_to_md` (local mode, **no LLM key**) | HTML→clean markdown (code blocks, tables) | in-process | always, after any HTML acquisition |
| 3 | **Crawl4AI** `AsyncWebCrawler` | headless Chromium render | 2–20s/page | JS-only pages, thin base-page content |
| 4 | **httpx** | raw everything | <0.5s | raw-text mirrors (`.txt`, `.md.txt`, `llms.txt`) + last-resort body fetch |

Ladder code shape (see `starter/fetch_stack.py` for the full file):

```python
async def fetch_one(url):
    # 0) raw-text mirrors: no rendering, ever
    if url.endswith(".txt"):
        async with httpx.AsyncClient(follow_redirects=True, timeout=25) as c:
            resp = await c.get(url); resp.raise_for_status()
            return {"url": url, "title": url.rsplit("/",1)[-1], "html": "",
                    "markdown": f"# {url}\n\n{resp.text}", "links": [], "method": "httpx-raw-text"}
    # 1) Scrapling fetch + 2) scrapegraph convert
    try:
        r = await asyncio.to_thread(scrapling_fetch, url)          # → html, links, title
        main_html, title2 = extract_main_content_html(r["html"], url)
        md = scrapegraph_markdown(main_html, url)
        if len(md.strip()) < 800:                                  # thin-content gate
            raise RuntimeError(f"scrapling thin ({len(md)} chars)")
        return {..., "method": "scrapling+scrapegraph"}
    except Exception as e1:
        last = f"scrapling: {e1}"
    # 3) Crawl4AI headless + best-of markdown
    try:
        r = await crawl4ai_fetch(url)
        ... md_best = max(crawl4ai_md, scrapegraph(main_html), key=len)
        if len(md_best.strip()) < 200: raise ...
        return {..., "method": "crawl4ai+scrapegraph"}
    except Exception as e2:
        last = f"{last} | crawl4ai: {e2}"
    # 4) httpx last resort (or give up → record)
    r = await httpx_fetch(url); return {..., "method": "httpx-fallback"}
```

### Hard-won setup facts (do these or level 2 breaks)

- **scrapegraphai 1.36.0 pip install is broken** for convert-only use: stale `langchain.prompts` imports. Fix once — keep a patched clone under `tools/scrapegraph-ai/`:
  ```bash
  git clone https://github.com/ScrapeGraphAI/Scrapegraph-ai tools/scrapegraph-ai
  sed -i 's/langchain\.prompts/langchain_core.prompts/g; s/langchain\.output_parsers/langchain_core.output_parsers/g' \
      tools/scrapegraph-ai/scrapegraphai/utils/convert_to_md.py
  # ignore playwright/LLM extras of the package; convert_to_md + cleanup_html are pure-python
  # load by FILE PATH so pip's broken copy can't shadow it:
  spec = importlib.util.spec_from_file_location("sg_md", TOOLS / "scrapegraph-ai/scrapegraphai/utils/convert_to_md.py")
  ```
- **crawl4ai needs chromium**: `python -m playwright install chromium` once per machine.
- **Keep ONE AsyncWebCrawler** (`global _crawler`, start once, reuse) — browser startup is the single biggest cost; and **wrap `arun` with `asyncio.wait_for(..., timeout=75)`**: `page_timeout=45000` does NOT protect against browser-level deadlocks (learned on `status.fish.audio` incident pages — curl fetched them in 1s while Chromium hung 16min). 📎 `fish-audio-docs` watchdog commit.
- Pinned versions that all seven sites run on: `requirements.txt` in any example repo.

### What NOT to use

- CloudScraper/undetected-chromedriver for doc sites — unnecessary; stealth headers suffice (robots.txt `Content-Signal` respects `ai-train=yes` on all seven).
- LLM-based extraction (ScrapeGraphAI has LLM modes) — offline `convert_to_md` is deterministic, free, fast.
- Selenium — crawl4ai wraps playwright better.

---

## 2. RECON — 20 minutes before any code

Run exactly this and write the answers down (they parameterize everything):

```bash
SITE=docs.example.com                                        # ← target
curl -sL https://$SITE/robots.txt                            # 1 sitemap listed? disallows? Content-Signal?
curl -sL https://$SITE/sitemap.xml -o /tmp/sm.xml            # 2 urlset or sitemapindex? N <loc>?
grep -c "<loc>" /tmp/sm.xml
grep -c "<lastmod>" /tmp/sm.xml                              # 3 lastmod → incremental refresh quality
grep -o "<loc>[^<]*</loc>" /tmp/sm.xml | sed 's/<[^>]*>//g' | sed 's#https\?://##' | cut -d/ -f2 \
  | sort | uniq -c | sort -rn | head                         # 4 locale dupes? (comfy: 7019 → 1806 en canonical)
curl -s -o /dev/null -w "%{http_code}\n" https://$SITE/llms.txt; curl -s -o /dev/null -w "%{http_code}\n" https://$SITE/llms-full.txt   # 5 raw mirrors?
curl -sL https://$SITE/llms.txt | head                       # 6 curated agent index? .md / _llms/*.txt pointers?
```

Then decide and record:

| Question | Decision rule |
|---|---|
| Host allowlist | MAIN_HOST + which subdomains? **Watch in-page link trees**: comfy leaked nothing because its allowlist had no giant domains; ai.google.dev *wanted* `developer.android.com` depth-1 (Java refs) — but cap it or it becomes 100k pages. Cross-domain references that are 100k+-page universes (developer.android.com gradle refs) get a `--max-pages` guard. 📎 ai-docs |
| Locale handling | strip `/zh\|/ja\|/ko…/` path prefixes in `normalize_url` keep English canonical only (comfy 7,019→1,806) |
| Binary skip list | png jpg jpeg gif svg webp ico css js woff woff2 ttf mp4 webm **zip pdf** — record, don't fetch (GCP: 64 PDFs recorded) |
| Auth-walled | console.*, admin.*, app dashboards → record-as-failed, never crawled |
| Raw mirrors | `*.txt`/`.md.txt`/llms files → level-4 raw fetch; mintlify `_llms/en/*.txt` section files are assets, not pages |
| State model | <10k pages → `crawl_state.json`; >10k → sqlite `crawl_state.db` (WAL) — GCP's is 320MB and does NOT live in git |
| Size/runtime estimate | pages ÷ (2–15 pages/s on 2 cores) → hours; markdown ≈ 2–8KB/page → GB; **corpus > ~4k pages ⇒ plan Releases-not-git now** (§9) |

📎 Reference recons: GCP = 70-shard sitemapindex, 1,230,844 raw → 601,023 canonical; comfy = 7,019 → 1,806; fish = 175 flat.

---

## 3. CRAWLER DESIGN — build order (one file, ~600 lines)

Copy the appropriate sibling repo's `crawl_*.py` (📎 ai.google.dev's `crawl_ai.py` is the generic one) and localize these eight pieces:

1. **Constants** — `BASE_URL / SITEMAP_INDEX / MAIN_LLMS / FULL_LLMS / MAIN_HOST / ALLOWED_ROOT_SUFFIXES / OUT_ROOT / STATE_FILE`.
2. **`normalize_url(u)`** — *the correctness core*. Order matters:
   - drop `#mailto:tel:javascript:data:`; `urljoin(base, u)`; strip fragment; scheme must be http(s)
   - **host gate**: `host == MAIN_HOST or endswith(allowed suffixes)` → keep, else `None` (delete any inherited giant-domain "pass" branch)
   - binary-extension reject (keep some `.txt`/`llms` cases allowed via the raw-text branch)
   - canonical path fix: `/index.md`→`/`, exact `/index`→`/` **segment-wise**, `*.md`→strip suffix, trailing-slash rule = *append `/` unless last segment contains a dot* (watch dot-slugs like `EmptyAceStep1.5LatentAudio/`)
   - locale prefix strip; query policy (docs: drop; or hash if needed)
3. **`url_to_relpath(url)`** — same sanitize rules → `<root>/<path>/... .md`; `_subdomains/<host>/` for non-main hosts; `.txt` mirrors keep their name; 120-char segment guard + md5 tail; query-hash suffix when needed.
4. **`extract_main_content_html`** — kill `script/style/noscript/svg`; `main`→`article`→`#main-content`→body fallback; title from `<title>`.
5. **`scrapegraph_markdown(html, url)`** — patched-clone `convert_to_md`; fallback `html2text` if the clone is missing (never fail a page because of the converter).
6. **`crawl4ai_fetch`** — singleton crawler, `CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=45000, wait_until="domcontentloaded", excluded_tags=[nav,footer,header,aside,script,style])`, `BrowserConfig(headless=True, verbose=False)`, **wait_for watchdog**, best-of markdown vs scrapegraph.
7. **`fetch_one`** — the ladder + raw-text branch + thin gates + 1 retry: wrap the whole ladder in `for attempt in range(2)` with a short backoff for 5xx/timeout/empty; **record everything in `failed` on final failure** (never silently drop).
8. **Main loop + state**:
   ```python
   seeds: sitemap (index→urls) + llms links + optional link-expansion (depth≤1 for reference hosts!)
   async with per-url: skip if visited in state; save page+meta+index; update state (commit per page or small batch); log one line per page to crawl.log; STATUS.log every 30s
   max-pages cap via --max-pages; concurrency via asyncio.Semaphore(--concurrency)
   ```
   sqlite for big sites: `visited(url, file, title, method, fetched_at)`, `failed(url, err)`, `shards(name, done)`, `PRAGMA journal_mode=WAL`.
   Heartbeat: `Status.log` append-only + `Status.json` (`{"visited":N,"failed":N,"shards":"x/70","updated":ISO,"done":bool}`) — cheap crash-resume indicator.

**Do not** expand links unboundedly: sitemap⊂site is the contract; expansion is recovery for pages missing from sitemap only.

### Sharding for monster sitemaps (GCP pattern) 📎 google-cloud-docs

`inventory.py`: fetch `sitemap.xml` (the index) → N shard files → write `seeds/sitemap_N_of_70.txt` per shard + resumable `seeds/_progress.json` (crash-safe). `crawl_gcp.py` then iterates shards; after each shard completes, **re-queue its failed/missing URLs** (rescan loop) before moving on. This is the only layout that survived a 1.23M-URL, 24-hour crawl plus spot-VM evictions.

---

## 4. RUN, SMOKE FIRST

```bash
python3 crawl_<site>.py --max-pages 25 --concurrency 8        # 1–2 min; check method field mix + 0-fail
tail -f crawl.log
python3 crawl_<site>.py --concurrency 12                      # full (resumable: re-run skips visited)
```

Success fingerprint: `failed` ≈ 0–1%, `method` field mostly `scrapling+scrapegraph`, a few `crawl4ai`, `httpx-raw-text` only for mirrors. Then:

```bash
find docs -name '*.md' | wc -l                                # matches recon estimate?
du -sh docs/                                                  # size in-band estimate (§2)
```

---

## 5. VERIFY COVERAGE — never trust "done" without an audit

The crawler's own bookkeeping lies sometimes (crashes mid-batch, locale canonicalization drift). Independent audit, **chunked** so 600k rows don't explode memory:

```python
# starter/audit.py (full file provided)
urls = {canon(l) for f in seeds/*.txt for l in f if l.strip()}   # use the CRAWLER's normalize, not your own regex!
have = chunked SELECT url FROM visited WHERE url IN (...3000/batch...)
missing = urls - have                       # then separate "recorded failed" from "never attempted"
```

Two audit rules learned the hard way:
1. **Reuse the crawler's `normalize_url` inside the audit** — a hand-rolled trailing-slash/file-extension heuristic False-positives on dot-slugs (`boogu-image-0.1`, `EmptyAceStep1.5LatentAudio`).
2. Coverage = `(visited ∪ failed_recorded) / sitemap_canonical`. Chasing the remainder: `final_gapfill.py` refetches `missing_urls.txt` using the SAME `fetch_one` + save format, then re-audit. GCP ended at **601,023/601,023 visited-or-recorded; missing=64 all PDF binaries.** Comfy picked up 1 recovered page from 7 "final" failures on retry — one retry round is mandatory.

Print a per-type breakdown of the remainder (`html/pdf/txt`) — PDFs/binaries should be *recorded*, not chased.

---

## 6. BACKEND OPERATIONS — make it survive reality

Durability over beauty.

```bash
# /data is its own disk (or mounted volume): survived every reboot/eviction we threw at it
/data/opencode/<site>/{crawl.py,update_incremental.py,seeds/,docs/,STATE,logs}
```

### systemd units (mirror-file them into the repo and copy on every boot)

```ini
# crawl.service  — Type=simple, Restart=on-failure   ⚠ NEVER Restart=always:
#   📎 ai-docs incident: 'always' relaunched a FINISHED crawl on every boot loop
# update.service — Type=oneshot, TimeoutStartSec=7200
# update.timer   — OnCalendar=…:30, RandomizedDelaySec=600, Persistent=true (catches up offline time)
```

```bash
systemctl enable --now <site>-update.timer   # crawls start manually; updates run forever
systemctl list-timers; journalctl -u <site>-update -n 100
```

### Idempotent `boot.sh` (recovery from reallocation; cloud-init runs it once per new VM)

- copy unit files → `daemon-reload` → `enable` → start crawl **only if no `pgrep -f crawl_x`** and not already `done`
- guard `RequiresMountsFor=/data` in units

### Concurrency sizing (2-core box measured)

| corpus | concurrency | result |
|---|---|---|
| ≤2k pages | 10–15 | minutes |
| 4–6k | 12–15 | 1–3 h |
| 600k | 20–30 | 18–24 h; drop to 20 when disk burns hot |
| RAM | | 607k-row visited cache ≈ 350MB; WAL mode mandatory |

### Watch the disk + git size trap while designing, not after

GCP docs = **12GB** → keep `docs/` gitignored from day one of a big site (§9).

---

## 7. INCREMENTAL UPDATES — "new + changed only, never recrawl"

`update_incremental.py` (📎 `ai-docs`' is the reference) — same save format as the crawler:

1. **Live sitemap** (+ `lastmod`). ⚠ whitespace-tolerant regex — GCP puts `<lastmod>` on its own line:
   `re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", text)` — adjacency-strict version reads 0 URLs (our first dry-run test literally proved 0).
   Track URL-set and lastmod-dict **separately**; urls never go missing because of lastmod shape.
2. Diff against state: `new = live_urls - visited` (crawled all; no cap), `stale = lastmod > fetched_at` sorted newest-first.
3. Refresh caps — **caps must never become deadlocks**: default drain (600/run), print guidance + backlog size when >5,000; `--refresh-cap 4000` for fast catch-up; only at >300k stale suggest "site overhaul → full redo". Never `abort()` silently which would stall timers for weeks (first version did exactly that: caught in testing).
4. Refetch through the SAME `fetch_one` ladder → overwrite page + `_meta` + `_index` line (update, not append-dup) → update sqlite/json state → daily **stamp files**:
   `docs/_last_verified.json` `{"date","at","new","refreshed",…}` + `_manifest.json` `last_verified` + regex-rewrite `Last verified: YYYY-MM-DD` in README(s).
5. `--check-only` = diff + guidance only, no writes. `--force URL` single-page redeploy.
6. `KNOWN_BROKEN` set for endpoints that 500 forever so daily runs don't resurrect error pages.

First-run reality: Google cloud.com regenerated `lastmod` for **54,911 pages (9%)** the day after our crawl — brands do this; your updater must shrug and drain.

---

## 8. GITHUB AUTOMATION (end to end)

### 8.1 Create repo + initial push

```bash
export GH_TOKEN=ghp_xxx        # user's own; revoke-able; NEVER lands in git
name='my-docs'; desc='End-to-end crawl of docs.example.com (Scrapling + Crawl4AI + ScrapeGraphAI)'
curl -s -X POST -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/user/repos \
  -d "$(python3 -c "import json,sys;print(json.dumps({'name':sys.argv[1],'description':sys.argv[2],'private':False}))" "$name" "$desc")"

git init -b main  &&  git config user.name ajithvnr2001  &&  git config user.email ajithvnr2001@users.noreply.github.com
git add .  &&  git commit -m 'End-to-end crawl of docs.example.com + daily Actions + GUIDE'
git remote add origin https://github.com/ajithvnr2001/$name.git
git push "https://ajithvnr2001:${GH_TOKEN}@github.com/ajithvnr2001/${name}.git" main
unset GH_TOKEN
```

### 8.2 What a docs-repo ships (full file set)

```
README.md            (Top: "> Last verified: <stamp-date>"  — the Action rewrites this line daily)
GUIDE.md             (per-site redo instructions; see §10 for the recipe and §11 for the failure table)
requirements.txt     (pinned from a real green machine)
.gitignore           (*.log crawl_state.json tools/ .venv __pycache__)
crawl_<site>.py      update_incremental.py
guide_code/          coder_scrape.py coder_update.py README.md   (single-page coder entry)
<configs>/           crawl.service update.service update.timer boot.sh cloud-init.yaml
.github/workflows/update-docs.yml                                   ← the automation
docs/                (tracked only if §9 says small; else ignored + Released)
```

### 8.3 The workflow — copy `starter/update-docs.yml`, and understand each choice

```yaml
on:
  schedule:
    - cron: "MM HH * * *"       # stagger the family: cf 02:00, ck 03:00, ai 04:00, VMs 05:30/06:30, comfy 07:00, fish 07:30
  workflow_dispatch: {}          # manual trigger = your test button
permissions: { contents: write } # Actions[bot] commits
concurrency: { group: daily-docs-update, cancel-in-progress: false }   # serial, never cancel mid-push
timeout-minutes: 360            # big refresh days
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5, python 3.11
  - run: pip install -r requirements.txt && python -m playwright install chromium
  - run: python3 update_incremental.py --concurrency 10
  - name: Commit and push if changed
    run: |
      git config user.name "github-actions[bot]" ... (git identity for the bot)
      git add docs/ README.md guide_code/README.md
      git diff --cached --quiet && echo "no changes" || (
        git commit -m "chore: daily docs update $(date -u +%F)"
        git push || (                            # ← RACE: a sibling cron may have pushed first; then:
          git fetch origin main && git rebase origin/main
          || (git checkout --ours -- docs/_last_verified.json docs/_manifest.json &&  # ← stamps are heartbeat files;
              git add -u && GIT_EDITOR=true git rebase --continue)                   #   your fresh copy wins, theirs dies
          && git push))
```

Why the rebase dance: two of the family's repos ran on the same minute once; both built commits from the same base; on conflict the *stamp/manifest* files were the cherry-stop; keep OURS (newer stamps are strictly better), and history stays linear-ish. Only two files ever conflict — the stamps.

### 8.4 TEST THE WORKFLOW (mandatory, 2 minutes after push)

Don't wait for tomorrow's cron — dispatch it immediately:

```bash
curl -s -X POST -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/ajithvnr2001/<repo>/actions/workflows/update-docs.yml/dispatches \
  -d '{"ref":"main"}'        # → 204 accepted (404 right after push = indexing lag; retry in 30–45s)
```

Then verify the RUN did the whole pipeline (not just "installed+no-op"):

```bash
curl -s -H "Authorization: Bearer $GH_TOKEN" .../actions/runs?per_page=1       # status/conclusion
curl -s .../actions/runs/<id>/jobs   | jq '.jobs[0].steps[] | {name, conclusion}'   # every step success?
curl -s .../commits?per_page=1       # expect "chore: daily docs update <today>" — proof stamp+push worked
```

📎 Both comfy-docs and fish-audio-docs produced a real `chore: daily docs update 2026-10-17` commit minutes after their first push — that's the acceptance signal, not just a green dot.

### 8.5 Reading the daily signal afterwards

- "no changes" most days = healthy corpus (+ stamp-only commit when `_last_verified.json` changes).
- growing `new` without commits = the push-retry broke; read the job log.
- `Last verified:` stuck on old date with green runs = your README lost the "Last verified:" line the regex rewrites.

---

## 9. BACKUP / GIT — when GitHub pushes REJECT a corpus

Git space is fine under ~4k pages but breaks at scale with two hard walls:

- per-file **100MB limit** (our `docs/_index.jsonl` hit 235MB) → `! [remote rejected] pre-receive hook declined`
- push totals >~5GB sputter; 600k-file trees also make `git status` a multi-minute syscall tornado — *never run it; commit by explicit path*

**Decision tree** (this is §2's "size" question answered operationally):

```
corpus small  → docs/ tracked in git, daily commit dance = your backup. (all seven except gcp)
corpus HUGE   → docs/ gitignored on day one; snapshots = GitHub Releases assets
                build_final_release.sh → 4 integrity-checked tar.gz trees, EACH ≤2GB/asset:
                  java/ sdk-langs/ _meta/ everything-else/   (GCP: 286+304+203+43MB)
                gh auth login --with-token <<<"$GH_TOKEN"
                gh release create docs-complete-$(date +%F) --title ... releases/*.tar.gz
                incremental deltas: updater --publish-delta → docs-delta-<date>.tar.gz tiny asset
```

 Consumers: "unzip 4 archives into one dir = full docs/ tree" needs a README map; deltas ship a `manifest.txt` of changed relpaths. Never `tar` while the crawler writes (torn pages → "file changed as we read it") — quiesce first.

**Local git index discipline for big trees:** `git rm --cached docs/` on 185k files takes minutes and times out naïvely (`timeout 280 git rm …` failed twice before a background run succeeded); verify with `git ls-files docs/ | wc -l` = 0 afterwards — a partial untrack leaves 185k tracked entries that later make `M docs/...` status noise for months (📎 we did this).

---

## 10. PER-SITE PARAM TEST — the "new site recipe" checklist

1. §2 recon answers on one page
2. `cp ai-docs/crawl_ai.py crawl_<site>.py` → constants + hosts + locale strip + raw-mirror types
3. smoke 25 → fix failure shapes → full run
4. audit + gapfill + one retry of failed
5. updater import switch + `KNOWN_BROKEN` + `REFRESH_CAP` sane for this site's churn
6. commit file-set §8.2 → push → **dispatch test §8.4**
7. enable VM timers (if this site uses VM runs), copy boot.sh/cloud-init into the repo
8. README stamp line + `Last verified:` mention of how the family divides crons

---

## 11. Failure mode taxonomy to test FOR (and what the tests caught)

| Failure | Found by | Sealed by |
|---|---|---|
| lastmod-on-separate-line → updater saw 0 URLs | dry-run `--check-only` (not eyeballing!) | whitespace regex + url/lastmod separate maps |
| refresh caps as abort-all → backlog 55k = updater stalls forever | same dry-run | drain semantics + guidance |
| `Restart=always` re-crawled finished corpora at every boot | VM reboot after finish | `Restart=on-failure` + boot.sh pgrep |
| Chromium deadlock (curl fast, browser eternal) — fish incident pages | 16-min no-progress window | `asyncio.wait_for(75)` watchdog + out-of-scope host exclusion |
| giant reference-domain link trees take over | ai.google.dev trip into 100k+ android pages | `--max-pages` cap + depth ≤1 |
| foreign-domain pass branch inherited from template | comfy audit (0 actually leaked; caught reviewing) | allowlist audit first, one-time |
| 100MB/_index pre-receive reject | release push for GCP | corpus→Releases, `_index.jsonl` never tracked |
| partial `git rm --cached` timeouts | tracked-file count anomalies | background run + `ls-files | wc -l == 0` verify |
| stamp rebase conflicts kill cron | two-cycle collisions in cf/ck family | `--ours` checkout of only the stamp files |
| duplicated pages from zip/locale | comfy raw 7,019 vs real 1,806 | canonical locale strip in `normalize_url` |

**The meta-rule:** dry-run every automated path the day you deploy it (`--check-only`, one `workflow_dispatch`, one `systemctl start`), because the "green" path is where most of these failures hid.

---

*Last verified on real corpus: 2026-10-17 — seven repos, 640k+ pages, every claim above came from a run that actually happened.*
