#!/usr/bin/env python3
"""TEMPLATE — independent coverage audit: never trust "done" without one.

Two rules encoded here:
  1. The audit MUST normalize with the crawler's own normalize_url
     (--crawler arg). A hand-rolled regex false-positives on dot-slugs
     (e.g. `EmptyAceStep1.5LatentAudio/`).
  2. Coverage = (visited ∪ failed_recorded) / sitemap_canonical. Binaries
     (.pdf/.zip) recorded-as-failed are covered BY DESIGN; print them as a
     separate bucket so nobody chases 64 PDFs for a week.

Chunked everywhere: sqlite has a host-parameter cap (<999 vars by default),
so every IN query runs in batches — the GCP 601k-URL audit ran this way.

  python3 audit.py --seeds seeds/ --state crawl_state.json
  python3 audit.py --seeds seeds/ --state crawl_state.db --crawler crawl_mysite.py --strict
  python3 audit.py --seeds seeds/ --state crawl_state.json --sitemap https://docs.mysite.com/sitemap.xml
"""
import argparse, importlib.util, json, re, sqlite3, sys, urllib.request
from pathlib import Path
from urllib.parse import unquote

ap = argparse.ArgumentParser()
ap.add_argument("--seeds", default="seeds/", help="dir of seed files (sitemap_*.txt, one URL per line)")
ap.add_argument("--sitemap", default=None, help="instead of --seeds: fetch a live sitemap (recursing indexes)")
ap.add_argument("--base", default=None, help="BASE_URL for normalization when using --sitemap")
ap.add_argument("--state", required=True, help="crawl_state.json OR sqlite crawl_state.db")
ap.add_argument("--crawler", default=None, help="crawl module exposing normalize_url — STRONGLY recommended")
ap.add_argument("--chunk", type=int, default=900, help="sqlite host-parameter batch size (<=999)")
ap.add_argument("--out", default="missing_urls.txt", help="where to write still-missing URLs")
ap.add_argument("--strict", action="store_true", help="exit 1 if anything (incl. binaries) is missing")
A = ap.parse_args()

# ---- normalize: borrowed from the crawler, or a conservative default ----
if A.crawler:
    spec = importlib.util.spec_from_file_location("C", A.crawler)
    C = importlib.util.module_from_spec(spec); spec.loader.exec_module(C)
    canon = lambda u: C.normalize_url(u)
else:
    base = A.base
    if base:
        canon = lambda u: (u if u.startswith("http") else base.rstrip("/") + "/" + u.lstrip("/"))
        print("[warn] no --crawler: trusting seeds are already canonical — "
              "prefer passing the crawl module so dot-slug paths audit correctly")
    else:
        canon = lambda u: u

def iter_seed_urls():
    if A.sitemap:
        yield from _live_sitemap(A.sitemap)
        return
    sroot = Path(A.seeds)
    if not sroot.exists():
        sys.exit(f"no seeds dir: {sroot} (pass --sitemap instead, or run crawl inventory)")
    for f in sorted(sroot.glob("*.txt")):
        if f.name.startswith("_"):
            continue
        for line in f.read_text(errors="replace").splitlines():
            if line.strip():
                yield line.strip()

def _live_sitemap(url, depth=0):
    with urllib.request.urlopen(url, timeout=60) as r:
        txt = r.read().decode("utf-8", "replace")
    if "<sitemapindex" in txt and depth < 2:
        for kid in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", txt):
            yield from _live_sitemap(kid, depth + 1)
    else:
        for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", txt):
            yield loc

def load_visited_failed_sqlite(dbpath, chunk):
    con = sqlite3.connect(str(dbpath))
    visited, failed = set(), {}
    # discover schema defensively: family sqlite has visited(url, ...) / failed(url, err)
    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for tab, is_visited in (("visited", True), ("failed", False)):
        if tab not in tabs:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({tab})")}
        url_col = next((c for c in ("url", "src", "seed") if c in cols), None)
        if not url_col:
            continue
        rows = [r[0] for r in con.execute(f"SELECT {url_col} FROM {tab}")]
        if is_visited:
            visited.update(rows)
            continue
        err_cols = [c for c in (("err", "error", "reason") if cols & {"err", "error", "reason"} else ())]
        for i in range(0, len(rows), chunk):       # chunked IN: sqlite host-parameter cap <999
            batch = rows[i:i + chunk]
            if err_cols:
                q = (f"SELECT {url_col}, {err_cols[0]} FROM {tab} WHERE {url_col} IN (%s)"
                     % ",".join("?" * len(batch)))
                for u, e in con.execute(q, batch):
                    failed[unquote(u)] = (e or "unknown")[:80]
            else:
                for u in batch:
                    failed[unquote(u)] = "unknown"
    con.close()
    return visited, failed

def load_visited_failed_json(jpath):
    st = json.loads(Path(jpath).read_text())
    vs = st.get("visited", [])
    if isinstance(vs, list):     # legacy: list of url strings
        return set(vs), {}
    fs = st.get("failed", {})
    return set(vs.keys()), {k: str(v)[:80] for k, v in fs.items()}

def main():
    # build the canonical seed set (streamed, deduped)
    seeds = set()
    raw = 0
    for u in iter_seed_urls():
        raw += 1
        c = canon(u)
        if c:
            seeds.add(c)
    print(f"seeds: {raw} raw → {len(seeds)} canonical")

    p = Path(A.state)
    if not p.exists():
        sys.exit(f"state file not found: {p}")
    try:
        visited, failed = load_visited_failed_sqlite(p, A.chunk)
        res = "sqlite"
    except (sqlite3.DatabaseError, json.JSONDecodeError, UnicodeDecodeError):
        visited, failed = load_visited_failed_json(p)
        res = "json"
    print(f"state ({res}): visited={len(visited)} failed_recorded={len(failed)}")

    missing = sorted(u for u in seeds if u not in visited and unquote(u) not in {unquote(v) for v in visited})
    recorded_failed = sorted(u for u in missing if u in failed)
    still_missing = [u for u in missing if u not in failed]

    def bucket(u):
        low = u.lower()
        for ext in (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".mp4", ".webm"):
            if low.endswith(ext):
                return ext[1:]
        return "html/txt"

    from collections import Counter
    b = Counter(bucket(u) for u in still_missing)
    cov = len(seeds) - len(still_missing)
    print(f"coverage: {cov}/{len(seeds)} ({cov * 100 // max(len(seeds), 1)}%) | "
          f"recorded-failed={len(recorded_failed)} | still-missing={len(still_missing)}")
    print(f"still-missing breakdown: {dict(b)}  ← binaries recorded not fetched are CORRECT behavior")
    for u in still_missing[:20]:
        print(f"  MISS: {u}")

    Path(A.out).write_text("\n".join(still_missing) + ("\n" if still_missing else ""))
    print(f"wrote {A.out} ({len(still_missing)} lines)")
    if A.strict and cov < len(seeds):
        sys.exit(1)

if __name__ == "__main__":
    main()
