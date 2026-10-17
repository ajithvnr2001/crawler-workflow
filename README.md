# Crawler Workflow — end-to-end playbook for doc-site crawling at any scale

Field-tested playbook distilled from **seven production crawlers** run to completion on a 2-core spot VM — cloudflare (21k pages), copilotkit (4.5k), sarvam (1.1k), ai.google.dev (5.8k), docs.cloud.google.com (**610k pages, 1.23M-URL sitemap**), comfy (2.2k), fish.audio (200) — with daily GitHub-Action incremental updates driving most of them.

**Read in this order:**

| Doc | What it covers |
|---|---|
| [`WORKFLOW.md`](WORKFLOW.md) | **The playbook**: which stack + why → recon → crawler design → verification → VM/systemd backend → incremental updates → GitHub automation → releases for oversized corpora → test checklist → troubleshooting (real bugs included) |
| [`PER-SITE-PARAMS.md`](PER-SITE-PARAMS.md) | Parameter sheet + cron map for all seven sites (dedupe, caps, automation, corpus handling) |
| [`starter/`](starter/) | Minimal working templates: fetch stack, incremental updater, Actions workflow, coverage audit — the skeleton of every new site |

**Live reference repos** (each repo has its own `GUIDE.md` redo section):
[`cloudflare`](https://github.com/ajithvnr2001/cloudflare) ·
[`copilotkit-docs`](https://github.com/ajithvnr2001/copilotkit-docs) ·
[`sarvam-docs`](https://github.com/ajithvnr2001/sarvam-docs) ·
[`ai-docs`](https://github.com/ajithvnr2001/ai-docs) ·
[`google-cloud-docs`](https://github.com/ajithvnr2001/google-cloud-docs) ·
[`comfy-docs`](https://github.com/ajithvnr2001/comfy-docs) ·
[`fish-audio-docs`](https://github.com/ajithvnr2001/fish-audio-docs)

**The 10 steps in one glance:**

```
1 recon (robots/sitemap/llms/locale/walls)   6 verify coverage (audit + gap-fill)
2 size it & pick state model                 7 install backend (systemd /data durable)
3 write crawler (fallback chain)             8 incremental updater (new + lastmod)
4 smoke test 25 pages                        9 GitHub repo + daily Actions workflow
5 full crawl (resumable)                    10 dispatch-test the workflow, then let cron take over
```

Status/versions pinned: `scrapling 0.4.15 · crawl4ai 0.9.4 · scrapegraphai 1.36.0 · playwright 1.63`
