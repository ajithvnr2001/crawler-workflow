# starter/ — four working templates, smoke-tested on the live family sites

| File | What it is | Smoke test |
|---|---|---|
| `fetch_stack.py` | The 4-level fetch ladder + canonicalization + relpath (minimal crawler core) | ✅ real page → `docs.fish.audio/agents/build/system-tools/` saved via `scrapling+scrapegraph` |
| `update_incremental.py` | Daily updater (new + lastmod-stale, drain semantics, stamps) — imports your crawl module | ✅ `--check-only` against `crawl_fish.py`: live sitemap 175 → 175 canonical |
| `update-docs.yml` | Daily GitHub Action: install → update → commit → push-with-rebase-dance | ✅ same YAML runs cron+dispatch in 5 family repos |
| `audit.py` | Independent coverage audit (chunked sqlite IN-batches, borrows the crawler's `normalize_url`) | ✅ json + sqlite state, PDF-recorded bucketing |

## Try the ladder on a real site without editing a file

```bash
CRAWL_BASE_URL=https://docs.fish.audio \
CRAWL_MAIN_HOST=docs.fish.audio \
CRAWL_ALLOWED_SUFFIXES=.fish.audio \
CRAWL_EXCLUDED_HOSTS=status.fish.audio \
CRAWL_OUT=/tmp/docs \
python3 fetch_stack.py https://docs.fish.audio/agents/build/system-tools/
```

## Diff your site against its live sitemap without writing anything

```bash
python3 update_incremental.py --crawler ../crawl_mysite.py --check-only
```

## Audit coverage against a json or sqlite state

```bash
python3 audit.py --seeds seeds/ --state crawl_state.json                       # small site
python3 audit.py --seeds seeds/ --state crawl_state.db --crawler ../crawl_mysite.py
python3 audit.py --sitemap https://docs.mysite.com/sitemap.xml --base https://docs.mysite.com --state crawl_state.json
```

The updater expects a crawl module exposing: `normalize_url`, `url_to_relpath`,
`fetch_one`, `OUT_ROOT`, `STATE_FILE`, `_crawler` (`REPO_ROOT` optional → README
stamp). `ai-docs`/`comfy`/`fish-audio` crawlers in the family all satisfy it.

Known template limits (fine for smoke + small sites, see WORKFLOW.md for the
production upgrades): no sitemap sharding, no sqlite STATE (json only), no
link expansion, single-node concurrency only.
