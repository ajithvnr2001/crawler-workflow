#!/usr/bin/env python3
"""TEMPLATE — daily incremental updater: NEW urls (always) + lastmod-stale
refresh (newest-first, capped, NEVER-ABORT drain semantics).

Single source of truth: it imports YOUR crawler module, so the fetch ladder,
canonicalization, file naming and save format can never drift apart.

  python3 update_incremental.py --crawler crawl_mysite.py --check-only
  python3 update_incremental.py --crawler crawl_mysite.py
  python3 update_incremental.py --crawler crawl_mysite.py --force https://docs.mysite.com/page/

Crawler-module contract (what it must expose):
  normalize_url(u)            url_to_relpath(url)        fetch_one(url) [async]
  OUT_ROOT (Path)             STATE_FILE (Path)          _crawler (AsyncWebCrawler or None)
  REPO_ROOT (Path) optional → enables README "Last verified" stamping
"""
import argparse, asyncio, importlib.util, json, pathlib, re, sys
from datetime import datetime, timezone
from urllib.parse import unquote

REFRESH_CAP = 100          # stale refetches per run (newest first) — raise via --refresh-cap
SEVERE_BACKLOG = 5000      # at which backlog size we print redo guidance (but keep draining)
RETRY_ROUNDS = 1           # one retry round is mandatory: comfy recovered 1/7 "final" failures on retry

# URLs whose origin is broken (verified by hand): excluded from auto-update so the
# daily run doesn't resurrect an error page every day.
KNOWN_BROKEN = set()
# e.g. {"https://docs.mysite.com/known-500-endpoint/"}

ap = argparse.ArgumentParser()
ap.add_argument("--crawler", default="crawl_site.py", help="path to the site's crawl module")
ap.add_argument("--sitemap", default=None, help="override sitemap url (default <BASE>/sitemap.xml)")
ap.add_argument("--check-only", action="store_true", help="diff + guidance only, writes nothing")
ap.add_argument("--force", default=None, help="refetch a single URL and exit")
ap.add_argument("--refresh-cap", type=int, default=REFRESH_CAP)
ap.add_argument("--concurrency", type=int, default=10)
A = ap.parse_args()

spec = importlib.util.spec_from_file_location("C", A.crawler)
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
DOCS, STATE = C.OUT_ROOT, C.STATE_FILE

async def fetch_sitemap_text(url, client, depth=0):
    """Return concatenated urlset texts; recurses one level into sitemap indexes."""
    r = await client.get(url, timeout=60); r.raise_for_status()
    txt = r.text
    if "<sitemapindex" in txt and depth < 2:      # index → recurse children
        kids = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", txt)
        out = []
        for k in kids:                            # note: NOT parallel — be polite to the origin
            try:
                out.append(await fetch_sitemap_text(k, client, depth + 1))
            except Exception as e:
                print(f"[sitemap] child failed {k}: {e}")
        return "\n".join(out)
    return txt

async def live_sitemap():
    out, lastmod = {}, {}
    url = A.sitemap or (getattr(C, "SITEMAP_URL", None) or C.BASE_URL.rstrip("/") + "/sitemap.xml")
    import httpx
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        txt = await fetch_sitemap_text(url, client)
    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", txt)
    out["docs"] = sorted({n for u in locs if (n := C.normalize_url(u))})
    # ★ whitespace-tolerant: some sites (GCP) put <lastmod> on its own line after </loc>.
    #   An adjacency-strict regex reads 0 URLs and the whole refresh silently no-ops.
    for u, d in re.findall(r"<loc>\s*([^<]+?)\s*</loc>\s*<lastmod>\s*([^<]+?)\s*</lastmod>", txt):
        n = C.normalize_url(u)
        if n and (n not in lastmod or d > lastmod[n]):
            lastmod[n] = d
    return out, lastmod, len(locs)

def load_state():
    if STATE.exists():
        st = json.loads(STATE.read_text())
    else:
        st = {"visited": {}, "failed": {}}
    return st.get("visited", {}), st.get("failed", {})

def save_state(vs, failed):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"visited": vs, "failed": failed}))

async def main():
    visited, failed = load_state()
    live, lastmod, raw_n = await live_sitemap()
    print(f"sitemap: {raw_n} raw <loc> → {sum(len(v) for v in live.values())} canonical")
    new = {k: [u for u in v if u not in visited and unquote(u) not in {unquote(x) for x in visited}
               and u not in KNOWN_BROKEN] for k, v in live.items()}
    for k, v in new.items():
        if v:
            print(f"{k}: new={len(v)}")
            for u in v[:20]: print(f"  NEW: {u}")
    total_new = sum(len(v) for v in new.values())

    # stale = sitemap lastmod newer than our _meta fetched_at → newest first
    stale = []
    meta_root = DOCS / "_meta"
    if meta_root.exists() and lastmod:
        for mfile in meta_root.rglob("*.json"):
            try:
                m = json.loads(mfile.read_text())
            except Exception:
                continue
            u, fa = m.get("url"), m.get("fetched_at")
            d = lastmod.get(u) if u else None
            if not u or not fa or not d or u in set(sum(new.values(), [])) or u in KNOWN_BROKEN:
                continue
            try:
                if datetime.fromisoformat(d.replace("Z", "+00:00")) > datetime.fromisoformat(fa):
                    stale.append((d, u))
            except Exception:
                pass
    stale.sort(reverse=True)
    refresh = [u for _, u in stale[:A.refresh_cap]]
    tail = ("" if len(stale) <= SEVERE_BACKLOG else
            f" ⚠ backlog >{SEVERE_BACKLOG}; keep draining at cap (consider --refresh-cap "
            f"4000 — NEVER abort the timer or the backlog stalls updates for weeks)")
    print(f"stale={len(stale)} refreshing newest {len(refresh)} (cap={A.refresh_cap}){tail}")

    targets = ([A.force] if A.force else [u for v in new.values() for u in v] + refresh)
    if A.check_only:
        print("check-only: no writes")
        return
    if not targets:
        print("no new docs — up to date")

    ok = fail = 0
    sem = asyncio.Semaphore(A.concurrency)

    async def one(url):
        nonlocal ok, fail
        async with sem:
            for attempt in range(RETRY_ROUNDS + 1):
                try:
                    data = await C.fetch_one(url)
                    rel = C.url_to_relpath(url)
                    fp = DOCS / rel
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    now = datetime.now(timezone.utc).isoformat()
                    fm = (f"---\nurl: {url}\ntitle: {json.dumps(data['title'])[1:-1]}\n"
                          f"method: {data['method']}\nfetched_at: {now}\n---\n\n")
                    fp.write_text(fm + data["markdown"], encoding="utf-8")
                    mr = DOCS / "_meta" / rel.with_suffix(".json")
                    mr.parent.mkdir(parents=True, exist_ok=True)
                    mr.write_text(json.dumps({"url": url, "file": str(rel), "title": data["title"],
                                              "method": data["method"], "fetched_at": now}, indent=2))
                    tag = "refresh" if url in visited else "new"
                    visited[url] = {"file": str(rel), "title": data["title"], "method": data["method"]}
                    print(f"[{tag}] {url}")
                    ok += 1
                    break
                except Exception as e:
                    if attempt < RETRY_ROUNDS:
                        await asyncio.sleep(3)
                        continue
                    failed[url] = str(e)[:300]; fail += 1
                    print(f"[fail] {url}: {e}")

    if targets:
        print(f"fetching {len(targets)} docs ({total_new} new + {len(refresh)} refresh)...")
        await asyncio.gather(*(one(u) for u in targets))
        save_state(visited, failed)
        if C._crawler is not None:
            try: await C._crawler.close()
            except Exception: pass
    else:
        save_state(visited, failed)   # still rewrite state so file exists

    # ---- daily verification stamps (committed even when nothing new) ----
    now = datetime.now(timezone.utc)
    stamp = {"date": now.strftime("%F"), "at": now.isoformat(),
             "new": {k: len(v) for k, v in new.items()},
             "refreshed": len(refresh), "stale_total": len(stale),
             "total_visited": len(visited)}
    (DOCS / "_last_verified.json").write_text(json.dumps(stamp, indent=2))
    try:
        mp = DOCS / "_manifest.json"
        m = json.loads(mp.read_text()) if mp.exists() else {}
        m["last_verified"], m["generated_at"] = stamp["date"], stamp["at"]
        mp.write_text(json.dumps(m, indent=2))
    except Exception as e:
        print("manifest stamp skip:", e)
    rr = getattr(C, "REPO_ROOT", None)
    if rr:
        for rp in [pathlib.Path(rr) / "README.md", pathlib.Path(rr) / "guide_code" / "README.md"]:
            try:
                if rp.exists():
                    t = rp.read_text()
                    t2 = re.sub(r"Last verified: \d{4}-\d{2}-\d{2}", f"Last verified: {stamp['date']}", t)
                    if t2 != t:
                        rp.write_text(t2)
                        print(f"readme date → {stamp['date']} ({rp})")
            except Exception as e:
                print(f"readme stamp skip ({rp}):", e)
    print(f"done: ok={ok} fail={fail} | stamped {stamp['date']}")
    if fail and ok == 0 and total_new:
        sys.exit(1)   # hard signal up the stack (and to Actions) that a run went 0-for-N

if __name__ == "__main__":
    asyncio.run(main())
