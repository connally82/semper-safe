# Semper Safe — Frontend

Multi-domain workbench for the Semper Safe sensor fusion platform.
Vite + React 18 + TypeScript. The single-component implementation
(`src/Workbench.jsx`) is intentionally still JSX — converting 1,000+
lines to TypeScript is Phase 5 polish, not Phase 0 deploy work.

## Local dev

```bash
cp .env.example .env.local           # adjust VITE_API_BASE if needed
npm install
npm run dev                          # http://localhost:5173
```

By default the workbench tries `VITE_API_BASE` and falls back to embedded
synthetic scenario data if the backend is unreachable, so you can run
the UI without the backend up.

## Production build

```bash
npm run build                        # writes dist/
npm run preview                      # serves dist/ on :4173
```

## Vercel deployment

This is the project root for Vercel. Project settings:

- **Framework Preset:** Vite (auto-detected)
- **Root Directory:** `frontend`
- **Build Command:** `npm run build` (default)
- **Output Directory:** `dist` (default)
- **Install Command:** `npm install` (default)

Set in Vercel's Environment Variables:

| Key | Value | Environments |
|---|---|---|
| `VITE_API_BASE` | `https://semper-safe.fly.dev` | Production, Preview |

(Switch to `https://api.sempersafe.live` once the DNS record + Fly cert
are in place — see `docs/roadmap.md` Phase 0.)

## What's deliberately NOT here

- Tailwind / a CSS framework — Workbench uses inline styles. Keep it that
  way until polish Phase 5.
- TanStack Query — direct `fetch` is fine for current shape; introduce
  Query when subscriptions land in Phase 3.
- MapLibre GL — listed in the roadmap stack but not used yet. Add when
  the embedded SVG map gets too small for real AIS density (Phase 2+).
