# PER-SITE PARAMS — the seven-site family, side by side

| Site | Raw sitemap → canonical | Final pages | Locale dedupe | State model | Automation | Corpus in git? |
|---|---|---|---|---|---|---|
| developers.cloudflare.com | 21k → 21k (single locale policy: en) | **21,290+** | n/a (skip non-en) | `crawl_state.json` | GitHub Actions **02:00 UTC** | ✅ in git |
| docs.copilotkit.ai | 4.5k → 4.5k | **4,554** | n/a | `crawl_state.json` | GitHub Actions **03:00 UTC** | ✅ in git |
| ai.google.dev | 6,854 → 653 (`?hl=` params dropped) | **5,780** (+android/chrome/aistudio refs, depth ≤1, capped) | query-locale | `crawl_state.json` | GitHub Actions **04:00 UTC** | ✅ in git |
| docs.cloud.google.com | **1,230,844 → 601,023** (70 shards; dup/version pairs collapse) | **610,208 visited** (incl. 2,601 next-day new + 4,000 refreshed, 64 PDFs recorded) | version-pair canonicalization | **sqlite, 320MB, WAL** | VM `update.timer` **05:30 UTC** + `--publish-delta` | ❌ Releases (`docs-complete-…` + `docs-delta-…`) |
| docs.sarvam.ai | 533 → 533 (+llms subdomains) | **1,090** | n/a | `crawl_state.json` | VM `update.timer` **06:30 UTC** (manual-initiated runs allowed; no Actions by design) | ✅ in git |
| docs.comfy.org | **7,019 → 1,806** (`/zh\|/ja\|/ko` path prefixes stripped) | **2,175** (2,169 md on disk; 7 dead-link records → 1 recovered on retry) | **path-locale** | `crawl_state.json` | GitHub Actions **07:00 UTC** | ✅ in git |
| docs.fish.audio | 175 → 175 | **200** (`status.fish.audio` excluded after hang) | n/a | `crawl_state.json` | GitHub Actions **07:30 UTC** | ✅ in git |

**Cron stagger map:** `02:00 cloudflare → 03:00 copilotkit → 04:00 ai → 05:30 gcp(VM) → 06:30 sarvam(VM) → 07:00 comfy → 07:30 fish` — no two jobs share a minute; VM-side timers use `Persistent=true` to catch up after downtime.

## Param picks that differ per site (copy-paste starting points)

| Param | Small static docs | Locale-heavy docs | JS-heavy docs | Reference-dump monster |
|---|---|---|---|---|
| fetch concurrency | 10–15 | 12 | 8 (more render falls) | 20–30 |
| state | json `visited: {}` | json | json | sqlite (sharded inventory) |
| max-pages guard | none | none | none | **mandatory + depth ≤1** |
| refresh cap (/run) | 100 | 100 | 100 | 600 (drain) / 4000 (catch-up) |
| corpus | git | git | git | Releases + deltas |
| llms.txt use | seed/snapshot | snapshot only (mintlify = section files) | seed + snapshot | n/a |

## The one script you should NOT copy between sites blindly

The host-allowlist branch inside `normalize_url` — every site's leaked-page incident traced back to an inherited "pass" branch. Audit it first, every time (§2 allowlist row, §10 recipe step 2).
