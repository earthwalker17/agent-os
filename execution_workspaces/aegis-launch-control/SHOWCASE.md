# Aegis Launch Control — built end-to-end by Agent OS

This folder is a **public showcase**: every line of the app under
[`repo/`](./repo) was written **autonomously by the Agent OS Coding Agent**,
starting from a completely empty repository, in response to a single
natural-language task card. No human wrote any of the application code, fixed
its build, or edited its files. It is committed here as a concrete example of
what Agent OS can do today.

> Every *other* project and workspace stays private (gitignored). Only this one
> demo is published, and only its source + run evidence — the regenerable
> `node_modules/`, `dist/`, and `logs/` are excluded.

## What got built

A "mission-control" planning dashboard — **React + TypeScript + Vite +
Tailwind** front end plus a **lightweight Express + JSON** back end:

- Six dashboard sections — **Mission Overview**, **Workstream Board**,
  **Dependency Map** (SVG node-edge graph), **Risk Radar** (impact/probability
  matrix), **Launch Timeline**, and an interactive **Scenario Simulator**.
- A realistic seed dataset (a mission, 6 workstreams, 41 tasks with
  dependencies, 15 risks, 8 milestones, 4 scenarios).
- A typed API client (`src/services/api.ts`) that fetches from the Express
  server and falls back to bundled static data when the API is offline.
- Tab navigation, responsive breakpoints, an error boundary, and a cinematic
  dark theme.

**30+ source and config files** under `repo/` (plus the run artifacts below).

## How it was built (the autonomous run)

One task card ([`runs/.../task_card.md`](./runs/20260619-044436-e65d2e61/task_card.md))
went in. Agent OS then, with **no further human input**:

1. **Planned** the work — decomposed the card into an **8-task graph** with
   dependencies (read-only inspection first), persisted as
   [`plan.json`](./runs/20260619-044436-e65d2e61/plan.json).
2. **Executed** the tasks one by one (scaffold → data models → each dashboard
   section → polish → backend API), writing every file through the sandboxed
   tool runtime.
3. **Verified** the result by actually running `npm install` + `npm run build`
   (TypeScript `tsc` + Vite) — **the build passed**, 41 modules transformed —
   with one automated repair pass along the way.
4. **Captured** a headless-browser screenshot of the running dev server.

Outcome: **8 / 8 tasks completed, 0 blockers, production build green.** See the
rendered [`result.md`](./runs/20260619-044436-e65d2e61/result.md) and the full
machine record [`run.json`](./runs/20260619-044436-e65d2e61/run.json).

### Full audit trail

Everything the agent did is replayable from the run artifacts in
[`runs/20260619-044436-e65d2e61/`](./runs/20260619-044436-e65d2e61):

| File | What it is |
|------|------------|
| `task_card.md` | The exact prompt the agent received |
| `plan.json` | The 8-task dependency graph it produced |
| `events.jsonl` | Chronological log of **every** tool call (file write, command, verification step) |
| `result.md` | Human-readable summary + the real `npm run build` log |
| `run.json` | The full structured run record |
| `screenshots/browser.png` | The automated browser-verification capture |

## About the screenshot

[`screenshots/browser.png`](./runs/20260619-044436-e65d2e61/screenshots/browser.png)
is the **automated** headless capture from browser verification, and it shows
the app's **loading state**, not the populated dashboard. That's a known,
documented limitation of single-server preview — not a build failure:

- Browser verification starts only the **frontend** dev server (`npm run dev`).
- This particular build wired the UI to fetch from the **separate Express API**,
  which that single-server preview doesn't start, so the capture landed during
  load.
- The app ships a static-data fallback for frontend-only use; the **production
  build itself passes** (see the build log in `result.md`).

To see it fully populated, run it yourself (below).

## Run it yourself

```bash
cd repo
npm install

# Full stack (frontend + Express API on its own port):
npm run dev:all

# …or frontend only (uses the bundled static-data fallback):
npm run dev
```

## Honest caveat

This is **AI-generated demo code**, committed verbatim from the autonomous run
(only `node_modules/` / `dist/` / `logs/` were excluded). It hasn't been
hand-reviewed or hardened for production — it's here to demonstrate Agent OS's
autonomous build capability, end to end, from an empty repo.
