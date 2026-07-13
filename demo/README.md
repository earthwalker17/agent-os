# Agent OS — product demo video

A ~56-second **silent** product-demo video that walks through Agent OS's core
flows using the **existing Pulseboard showcase records** (`sample-project`) — no
LLM re-run, no rebuild, no fabricated data.

**Watch it:** [youtu.be/KVtN_26qDYw](https://youtu.be/KVtN_26qDYw?si=lZ3hrhakmc8g2LOD)
(also featured at the top of the root [`README.md`](../README.md)).

**Output:** `agent-os-demo.mp4` — H.264 / MP4, 1280×720, 30 fps, ~56 s, ~5 MB, no
audio. This file (and the `raw/` + `build/` intermediates) is generated locally
by the scripts below and is **gitignored** — the published copy lives on YouTube,
so binary video never lands in the repo.

## What it shows

| Time | Beat | On-screen label |
|------|------|-----------------|
| 0–2.8s | Opening card | *A diff is not finished software.* · LLM + Harness = Agent |
| ~3–9s | Landing cockpit (memory, integrations, real Runs list) | Agent OS is the harness around the model. |
| ~9–17s | Chat thread: Main Agent plans & hands off, Coding Agent executes | Main Agent = brain · Coding Agent = hands |
| ~17–24s | Run detail: 7-task plan graph + audited event timeline | Controlled execution · auditable trace |
| ~24–31s | A real `npm run build` + browser + visual review, all PASSED | The model can't mark its own homework. |
| ~31–39s | A `partial` run: failing dev-server evidence → typed runtime recovery → linked child run | Failure → Evidence → Bounded recovery |
| ~39–47s | A two-phase Git **commit contract** (preview, never confirmed) | External actions require explicit approval. |
| ~47–52s | The finished, shipped Pulseboard app (real capture) | Pulseboard — planned, built, verified & shipped through Agent OS |
| ~52–56s | End card | Agent OS · LLM + Harness = Agent · github.com/earthwalker17/agent-os |

The three run records used are all real, on disk under
`execution_workspaces/sample-project/runs/`:
`20260708-025224-…` (plan + trace), `20260708-081514-…` (all-green verification),
`20260708-071842-…` (browser failure → runtime recovery + Git).

## How it was made

1. **`record.py`** drives the *live, locally-running* Agent OS cockpit with
   Playwright and records **seven short clips — one per beat, each in its own
   `BrowserContext` video** (`raw/*.webm`). Recording each beat separately keeps
   the video encoder from accumulating backlog across a long session (which
   stalls the browser on heavy scrolls); it writes `build/clips.json` with each
   clip's content window.
2. **`make_video.py`** trims each clip to its content window, burns in the beat
   label with FFmpeg `drawtext`, renders an opening and end card, and
   concatenates everything into `agent-os-demo.mp4`.

Fonts and label text live under `assets/`.

## Re-running

```powershell
# 1. Start Agent OS locally (backend :8000, frontend :5173)
cd backend;  python -m uvicorn main:app --port 8000     # terminal 1
cd frontend; npm run dev                                 # terminal 2

# 2. Record the beat clips, then assemble the MP4
cd demo
python record.py       # -> raw/*.webm + build/clips.json
python make_video.py    # -> agent-os-demo.mp4
```

Requires `ffmpeg` on PATH and the Playwright Chromium browser
(`python -m playwright install chromium`).

## Safety / integrity notes

- **Read-only.** The walkthrough only navigates (GET), opens modals, scrolls,
  and triggers one **local, non-mutating** Git commit *preview* (`confirm:false`)
  that is **never confirmed** — no push, deploy, migration, or payment. The
  showcase run records and database are byte-for-byte unchanged (verified).
- **No secrets exposed.** A small DOM redactor hides private account slugs
  (the email-derived Vercel slug, the Supabase project ref, the Stripe account
  id) while keeping the *public* GitHub org (`earthwalker17`) visible. No tokens,
  API keys, private filesystem paths, or private emails appear.
- **Nothing invented.** Every screen is real product state; the only added text
  is the labels/cards listed above.
- Production code and behavior are untouched — this folder is self-contained.
