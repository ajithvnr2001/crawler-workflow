#!/usr/bin/env python3
"""TEMPLATE — the 4-level fetch ladder every family crawler shares.

  1. Scrapling  (HTTP + stealth headers)              → 90–95% of static doc pages
  2. ScrapeGraphAI convert_to_md (offline, NO LLM key)→ clean markdown from HTML
  3. Crawl4AI headless Chromium (watchdog-wrapped)    → JS-only pages
  4. httpx                                            → raw-text mirrors (.txt)

Smoke-test it on a real site WITHOUT editing the file (env overrides):

  CRAWL_BASE_URL=https://docs.fish.audio
  CRAWL_MAIN_HOST=docs.fish.audio
  CRAWL_ALLOWED_SUFFIXES=.fish.audio
  CRAWL_EXCLUDED_HOSTS=status.fish.audio
  CRAWL_OUT=/tmp/docs
  python3 fetch_stack.py https://docs.fish.audio/

This file is the minimal correctness core (ladder + normalize + relpath).
For a full crawler (sitemap seeds, state, concurrency, _index.jsonl, STATUS
heartbeat) start from a family repo instead — `ai-docs/crawl_ai.py` is the
generic superset, cwd it into `crawl_<site>.py` and edit the constants.

Hard-won setup facts baked in below:
  * scrapegraphai 1.36.0 pip install imports stale `langchain.prompts` — the
    patched clone is loaded BY FILE PATH here (sed langchain→langchain_core
    in tools/scrapegraph-ai/scrapegraphai/utils/convert_to_md.py once).
  * `page_timeout` does NOT stop chromium deadlocks (fish incident page hung
    16 min in browser while curl fetched in 1s) → outer asyncio.wait_for watchdog.
"""
import asyncio, hashlib, importlib.util, json, os, re, sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, urldefrag, unquote

# ═══════════════ YOUR SITE — env-overridable for smoke tests ═══════════════
BASE_URL = os.environ.get("CRAWL_BASE_URL", "https://docs.example.com")
MAIN_HOST = os.environ.get("CRAWL_MAIN_HOST", "docs.example.com")
ALLOWED_SUFFIXES = tuple(s.strip() for s in os.environ.get("CRAWL_ALLOWED_SUFFIXES", ".example.com").split(",") if s.strip())
EXCLUDED_HOSTS = tuple(s.strip() for s in os.environ.get("CRAWL_EXCLUDED_HOSTS", "").split(",") if s.strip())
LOCALE_RE = re.compile(os.environ["CRAWL_LOCALE_RE"]) if os.environ.get("CRAWL_LOCALE_RE") else None
RAW_TEXT_SUFFIXES = (".txt",)   # fetched raw, never rendered
OUT = Path(os.environ.get("CRAWL_OUT", "./docs"))
BIN_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
           ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".zip", ".pdf")
# ════════════════════════════════════════════════════════════════════════════

# --- scrapegraph, loaded by FILE PATH so pip's broken copy can't shadow it ---
def _load_sg():
    for base in (Path(__file__).resolve().parent / "tools", Path("/data/opencode/cloudflare/tools")):
        fp = base / "scrapegraph-ai" / "scrapegraphai" / "utils" / "convert_to_md.py"
        if fp.exists():
            spec = importlib.util.spec_from_file_location("sg_md", fp)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m.convert_to_md
    import scrapegraphai.utils.convert_to_md as sg   # pip fallback (patch site-packages yourself)
    return sg.convert_to_md
_SG = _load_sg()

def scrapegraph_markdown(html, url):
    try:
        return _SG(html, url)
    except Exception:
        import html2text           # never fail a page because of the converter
        h = html2text.HTML2Text(); h.ignore_links = False; h.body_width = 0
        return h.handle(html)

# --- level 1: Scrapling (sync lib → run via asyncio.to_thread) ---
def scrapling_fetch(url):
    from scrapling import Fetcher
    p = Fetcher.get(url, stealthy_headers=True, follow_redirects=True, timeout=30)
    if p.status != 200:
        raise RuntimeError(f"scrapling status {p.status}")
    links = [a.attrib.get("href", "") for a in p.css("a")]
    t = p.css("title::text")
    return {"html": p.html_content, "links": links, "title": str(t[0]).strip() if t else ""}

def extract_main_content_html(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.find("div", {"id": "main-content"})
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if main:
        return str(main), title
    body = soup.find("body")
    return (str(body), title) if body else (html, title)

# --- level 3: Crawl4AI singleton + browser-level watchdog ---
_crawler = None

async def get_crawler():
    global _crawler
    if _crawler is None:
        from crawl4ai import AsyncWebCrawler, BrowserConfig
        _crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        await _crawler.start()          # start ONCE, reuse: browser boot is the biggest cost
        print("[crawl4ai] started")
    return _crawler

async def crawl4ai_fetch(url):
    from crawl4ai import CrawlerRunConfig, CacheMode
    cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=45000,
                           wait_until="domcontentloaded",
                           excluded_tags=["nav", "footer", "header", "aside", "script", "style"])
    # ★ the watchdog: browser deadlocks outlive page_timeout
    res = await asyncio.wait_for((await get_crawler()).arun(url=url, config=cfg), timeout=75)
    if not res or not res.success:
        raise RuntimeError(f"crawl4ai: {getattr(res, 'error_message', 'no result')}")
    return res.html or "", res.markdown or ""

# --- THE LADDER ---
async def fetch_one(url):
    if url.endswith(RAW_TEXT_SUFFIXES):
        async with httpx.AsyncClient(follow_redirects=True, timeout=25) as c:
            resp = await c.get(url); resp.raise_for_status()
            title = url.rsplit("/", 1)[-1]
            return {"url": url, "title": title, "html": "", "links": [], "method": "httpx-raw-text",
                    "markdown": f"# {title}\n\n> Source: {url}\n\n{resp.text}"}
    try:                                              # levels 1+2
        r = await asyncio.to_thread(scrapling_fetch, url)
        main_html, t2 = extract_main_content_html(r["html"], url)
        title = r["title"] or t2 or url
        md = scrapegraph_markdown(main_html, url)
        full = f"# {title}\n\n> Source: {url}\n\n{md}"
        if len(full.strip()) < 800:                   # thin-content gate → escalate to browser
            raise RuntimeError(f"thin content ({len(full)} chars)")
        return {"url": url, "title": title, "html": r["html"], "links": r["links"],
                "markdown": full, "method": "scrapling+scrapegraph"}
    except Exception as e1:
        last = f"scrapling: {e1}"
    try:                                              # level 3 (+ best-of markdown)
        html4, md4 = await crawl4ai_fetch(url)
        main4, t4 = extract_main_content_html(html4, url) if html4 else ("", "")
        md_best = max(md4 or "", scrapegraph_markdown(main4, url) if main4 else "", key=len)
        if len(md_best.strip()) < 200:
            raise RuntimeError("thin content after render")
        title = t4 or url
        return {"url": url, "title": title, "html": html4, "links": [],
                "markdown": f"# {title}\n\n> Source: {url}\n\n{md_best}",
                "method": "crawl4ai+scrapegraph"}
    except Exception as e2:
        last += f" | crawl4ai: {e2}"
    raise RuntimeError(f"all fetchers failed for {url}: {last}")

# --- canonicalization (family rules; audit MUST reuse this, never re-invent) ---
def normalize_url(u, base=BASE_URL):
    if not u: return None
    u = u.strip()
    if u.startswith(("#", "mailto:", "tel:", "javascript:", "data:")): return None
    abs_u, _ = urldefrag(urljoin(base, u))
    try:
        p = urlparse(abs_u)
    except Exception:
        return None
    if p.scheme not in ("http", "https"): return None
    host = p.netloc.lower().split(":")[0]
    # ★ keep the allowlist MINIMAL: every leak incident traced to an inherited "pass" branch
    if not (host == MAIN_HOST or host.endswith(ALLOWED_SUFFIXES)) or host in EXCLUDED_HOSTS:
        return None
    if p.path.lower().endswith(BIN_EXT): return None
    path = p.path
    if LOCALE_RE:
        path = LOCALE_RE.sub("", path)                # e.g. ^/(zh|ja|ko)(?=/|$) strips locale prefixes
    if not path: path = "/"
    # canonicalize indexes / *.md source links to their directory URL
    if path.endswith("/index.md"):      path = path[: -len("/index.md")] + "/"
    elif path.endswith("/index"):       path = path[: -len("/index")] + "/"
    elif path.endswith(".md"):
        path = path[:-3] + ("" if path[:-3].endswith("/") else "/")
    # drop query/fragment for docs; enforce trailing slash on directory URLs
    canon = f"https://{host}{path}"
    if not canon.endswith("/") and "." not in canon.rsplit("/", 1)[-1]:
        canon += "/"
    return canon

def url_to_relpath(url):
    p = urlparse(url)
    host = p.netloc.lower()
    query = p.query
    path = unquote(p.path).strip("/")
    if path.endswith("/index"):
        path = path[: -len("/index")].strip("/")
    parts = []
    for seg in (path.split("/") if path else []):
        seg = re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", seg).strip()
        if seg in ("", ".", ".."): seg = "_"
        if len(seg) > 120:
            seg = seg[:100] + "_" + hashlib.md5(seg.encode()).hexdigest()[:8]
        parts.append(seg)
    rel = "/".join(parts) if parts else "_index"
    if query:
        rel += "__q_" + hashlib.md5(query.encode()).hexdigest()[:8]
    sub = host != MAIN_HOST
    if rel.endswith(".txt"):    # raw-text mirrors keep their filename
        return Path(f"_subdomains/{host}/{rel}") if sub else Path(rel)
    return Path(f"_subdomains/{host}/{rel}.md") if sub else Path(rel + ".md")

async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else BASE_URL
    page = await fetch_one(url)
    rel = url_to_relpath(page["url"])
    fp = OUT / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fm = (f"---\nurl: {page['url']}\ntitle: {json.dumps(page['title'])[1:-1]}\n"
          f"method: {page['method']}\nfetched_at: {datetime.now(timezone.utc).isoformat()}\n---\n\n")
    fp.write_text(fm + page["markdown"], encoding="utf-8")
    print(f"saved {fp} via {page['method']} ({len(page['markdown'])} chars)")
    if _crawler is not None:
        try: await _crawler.close()
        except Exception: pass

import httpx  # noqa: E402  (import after constants so env-failures fail fast)
from bs4 import BeautifulSoup  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
