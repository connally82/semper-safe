# Semper Safe — Solo Build-Out Plan
### Evenings & weekends · 6 months to a deployed, real-data prototype

> Realistic, not aspirational. ~8–12 hours/week. Each phase ships
> something working before the next begins. No phase requires the
> previous to be "perfect" — only "deployed and not embarrassing."

---

## North Star

**Month 6:** A live web app at a real URL where anyone can sign up,
view live AIS + dark-vessel detections off any coast they choose,
and get email alerts when activity matches their filter. Maritime
domain only. Real data. Real users. Real (small) audit log.

**Month 12 (stretch):** Add wildfire domain plus flood-stage
monitoring. Land first SBIR Phase I award or first paid pilot.

If you hit Month 6, you've already built something 95% of "I have an
idea" people never get to. Stop and reassess at Month 6 before
committing to Month 12 — your actual interests and energy will tell
you which way to push.

---

## Operating principles for solo evenings-and-weekends

These are non-negotiable. Break them and the project dies:

1. **Ship every week.** Even if it's tiny. A deployed bug beats a
   perfect local feature. The dopamine of seeing it work is what
   fuels the next session.
2. **Boring tech wins.** Pick the most boring viable option for
   every infra decision. Postgres, not a vector DB. Server-rendered
   HTML where possible. One language for backend (Python). One for
   frontend (TypeScript + React). No Rust until you have paying
   customers asking for latency.
3. **Time-box ruthlessly.** If a task estimate exceeds 2 weekends,
   either decompose it or cut it. You can't carry 4-week tasks
   across life events without losing the thread.
4. **One domain at a time.** Maritime first. Don't let yourself
   touch wildfire or flood until maritime ships. The temptation to
   bounce between domains is the #1 killer of solo projects.
5. **Public from week 1.** GitHub repo public, README real, deploy
   URL shareable. Builds accountability. Forces you to write things
   that aren't embarrassing. Lets future-you remember what you did.
6. **One newsletter, one tweet, one Show HN every month.**  No
   audience-building hustle, just a forcing function to articulate
   what you've shipped. The narrative practice matters more than
   the audience.

---

## Six-month plan

The plan reads top to bottom by week. Each phase has an
**exit criterion** — if you can't say "yes" to it, don't move on.

---

### Phase 0 · Weeks 1–2 · Foundation (~20 hours)

**Goal:** Move from "scaffold on my laptop" to "deployed app at a
URL" with current synthetic data.

| Week | Tasks |
|------|-------|
| 1 | Set up GitHub repo, push current MVP. Add MIT license. Get a domain (`findandrespond.org` or similar — ~$12). |
| 1 | Sign up for: Fly.io (backend hosting), Vercel (frontend), Resend (email later), GitHub Actions (CI), Sentry (error tracking — free tier). |
| 2 | Containerize backend with Dockerfile. Deploy to Fly.io. Test from your phone. |
| 2 | Move React workbench into a Vite + TypeScript project. Deploy to Vercel. Connect to live backend. |
| 2 | Set up GitHub Actions: on push to main, run tests + deploy. Even with 1 trivial test, the muscle memory matters. |

**Stack pinned for the rest of the project:**
- Backend: Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2
- DB: PostgreSQL 16 + PostGIS extension (Fly.io managed Postgres)
- Frontend: Vite + TypeScript + React 18 + TanStack Query + MapLibre GL
- Auth: Clerk (free tier, works in 30 minutes)
- Email: Resend (3,000 free/month)
- Hosting: Fly.io backend, Vercel frontend
- Monitoring: Sentry (free), Logtail or Axiom for logs
- Domain: Cloudflare DNS

**Exit criterion:** A friend on a different network can hit your
URL, see the workbench, and click around. Total monthly cost: <$10.

---

### Phase 1 · Weeks 3–4 · Persistence (~16 hours)

**Goal:** Move from in-memory to PostgreSQL. Audit log persists.
Restarts don't lose state.

| Week | Tasks |
|------|-------|
| 3 | Add SQLAlchemy models mirroring `models.py` Pydantic types. PostGIS column for `geom`. Alembic migrations. |
| 3 | Rewrite `audit.py` to write to a Postgres table with a UNIQUE constraint on `seq` and `prev_hash`. The hash chain is enforced by the DB now. |
| 4 | Rewrite `FusionEngine` to load entities from DB on startup, persist on every change. Use a transaction per ingest. |
| 4 | Add a `/replay/{from_date}` admin endpoint that shows historical state. Bonus, but useful for demos. |

**Concept to learn this phase:** Database transactions and how
PostGIS spatial queries work. Read [Postgres docs on
indexes](https://www.postgresql.org/docs/current/indexes.html) and
[PostGIS in 10 minutes](https://postgis.net/workshops/postgis-intro/).

**Exit criterion:** Restart the backend mid-conversation. Refresh
the workbench. State is intact. Audit log shows the gap as a
process restart event but the chain still verifies.

---

### Phase 2 · Weeks 5–8 · Real AIS data (~32 hours)

**Goal:** Replace synthetic AIS with live data from
[AISStream.io](https://aisstream.io) (free WebSocket feed) for one
real area of operation.

| Week | Tasks |
|------|-------|
| 5 | Sign up at AISStream.io. Get API key. Test their WebSocket from a Python REPL. Read their docs. |
| 5 | Pick an area of operation: Pacific NW coast (good vessel density, useful conservation context, no IUU concerns to muddy your tests). Bound it: 47°–50°N, 122°–128°W. |
| 6 | Build an ingestion worker: a separate Python process subscribed to AISStream, normalizing each message into your `Observation` schema, calling `engine.ingest()`. Run it as a Fly.io machine. |
| 6 | Adjust the AIS-gap threshold for real data. Real vessels report at irregular intervals — your 15-min threshold may be wrong. Tune against your AOI's actual data. |
| 7 | Add a "viewer mode" — anyone can land on the workbench and see live AIS in your AOI without logging in. Read-only. This is your demo URL. |
| 7 | First "Show HN" / r/dataisbeautiful / Twitter post. "Live dark-vessel detection at $URL." Ship it. |
| 8 | Whatever broke under real-data load, fix it. The engine *will* misbehave on real data. Plan on it. |

**Concept to learn this phase:** WebSockets, async Python, and how
real AIS data is messy (incomplete messages, nav-status fields you
don't know, MMSIs that change, vessels off the coast of Antarctica
because someone fat-fingered a coordinate).

**Exit criterion:** Anyone in the world can hit your URL and see
the last hour of vessel activity in your AOI, with at least one
genuine "AIS gap" detection per day visible in the audit log.
This is what counts as a working product. Take a screenshot. Send
it to 5 people. Get one piece of feedback you didn't expect.

---

### Phase 3 · Weeks 9–10 · Auth + alerts (~16 hours)

**Goal:** Logged-in users can save AOIs and get email alerts.

| Week | Tasks |
|------|-------|
| 9 | Add Clerk auth to frontend. ~30 minutes of integration. |
| 9 | Add a `users` table linked to Clerk user ID. Add `subscriptions` table: `(user_id, aoi_polygon, entity_types[], delivery_method)`. |
| 10 | Build alert dispatcher: every 10 min, scan new high-priority entities for matches against subscriptions. Send via Resend. |
| 10 | Add audit entries for every alert dispatched: `actor=alert_system, event=alert_sent, payload={user_id, entity_id, channel}`. The system audits itself, not just operators. |

**Exit criterion:** You sign up your own account, draw an AOI, and
24 hours later have at least one real alert email in your inbox.
Then ask 3 friends to do the same. If even one of them stays
signed up after a week, you have a product.

---

### Phase 4 · Weeks 11–14 · SAR fusion on real data (~32 hours)

**This is the hardest phase. Budget extra time.**

**Goal:** Pull Sentinel-1 SAR scenes for your AOI, run dark-vessel
detection, fuse with AIS. The "wow" of the original demo, on real
data.

| Week | Tasks |
|------|-------|
| 11 | Register at [Copernicus Data Space](https://dataspace.copernicus.eu). Use their STAC catalog API to find Sentinel-1 IW scenes over your AOI. Download one manually, view it in QGIS to understand what you're looking at. |
| 12 | Use [`sentinelhub-py`](https://sentinelhub-py.readthedocs.io) or direct STAC + `rasterio` to download new scenes automatically. Schedule: every Sentinel-1 pass over your AOI (every ~6 days, twice a day during overlap). |
| 13 | Vessel detection: skip ML for v1. Use a CFAR (Constant False Alarm Rate) detector — classical SAR vessel detection that works without training data. ~150 lines of NumPy. [Reference paper.](https://www.mdpi.com/2072-4292/12/8/1305) |
| 14 | Fuse CFAR detections with AIS in your DB. Real dark vessels will appear. Most will be small fishing boats with broken transponders, not IUU — but the *fusion* works. |

**Concept to learn this phase:** SAR imagery basics, GeoTIFF
handling with `rasterio`, Constant False Alarm Rate detection.
Watch [SARLens YouTube intro](https://www.youtube.com/results?search_query=sentinel-1+sar+intro)
and read [ESA's S1 user guide](https://sentinel.esa.int/web/sentinel/user-guides/sentinel-1-sar).

**Exit criterion:** A real Sentinel-1 scene over your AOI gets
processed within 2 hours of acquisition. CFAR detections appear on
the workbench. At least one detection in a week genuinely lacks an
AIS match and gets flagged as a dark vessel candidate.

---

### Phase 5 · Weeks 15–18 · Polish, docs, distribution (~32 hours)

**Goal:** Make the thing actually presentable. This is when most
solo projects die — you've built it, but no one knows. Resist that.

| Week | Tasks |
|------|-------|
| 15 | Write a *real* landing page. Not the workbench — a marketing page that explains what this is, who it's for, and why. Include a 60-sec Loom demo. |
| 15 | Documentation site (Mintlify or Docusaurus). Architecture page. API reference. "Why phased deployment" page. |
| 16 | Public roadmap (GitHub Projects or a Notion page). Issue templates. Be welcoming to contributors. |
| 16 | First user research: email 10 people from NOAA, USCG, Global Fishing Watch, university marine labs. Ask for 20 minutes to show them the prototype. Expect 1–2 yeses. The ones who say yes will reshape your roadmap. |
| 17 | Whatever those calls revealed → prioritize one fix or addition. |
| 17 | Submit to: Show HN, r/programming, r/MachineLearning, r/geospatial, the OpenSAR community on Discord, TheStakeholders newsletter on Substack. |
| 18 | Buffer week. Catch up on the things that broke. Take a real break — you've earned it. |

**Exit criterion:** 50+ unique visitors in a week (Plausible Analytics).
At least one inbound message from a stranger saying "I'd actually
use this." If zero inbound, your distribution is the bottleneck,
not the product.

---

### Phase 6 · Weeks 19–24 · Pick your fork

By now you'll know what's working. Three honest paths:

**Path A — Wildfire domain.** You have the architecture. Plug
NASA FIRMS (free VIIRS) and GOES thermal. About 4 weeks. Doubles
your story for outreach. Especially relevant if you live somewhere
fire-affected.

**Path B — Flood domain (your interest).** This is the hardest of
the three but the most under-served. Sources:

- **USGS Water Services** — free real-time stream gauges, every
  river in the US. [waterservices.usgs.gov](https://waterservices.usgs.gov)
- **NOAA NWPS** — National Water Prediction Service flood forecasts
- **Sentinel-1 SAR** — water extent mapping (you already have this
  pipeline from Phase 4)
- **Global Flood Awareness System (GloFAS)** — global river forecasts
- **Citizen reports** — Twitter, Mastodon (carefully, lots of noise)

The fusion challenge is *time-evolving extent*: a flood is a
polygon that grows. Your `Entity` model needs a small extension
for time-stamped geometry. The "act" step is alerts to people in
the affected polygon, evacuation route advisories — high impact,
defensible civilian use case.

**Path C — Go deep on maritime.** Add SBIR-grade features: ML
vessel classifier, illegal-fishing scoring, country-of-origin
inference, MPA-violation alerts. Apply for SBIR Phase I from NOAA
or USCG. ~$150K if awarded. Also: contact Global Fishing Watch
about partnership.

The right path depends on what your Phase 5 calls revealed and
what's keeping you up at night. Pick **one**.

---

## Backend stack — concrete (pinned for the project)

```
Backend
├── Python 3.12
├── FastAPI         (you have it)
├── SQLAlchemy 2.0  (ORM)
├── Alembic         (migrations)
├── Pydantic v2     (you have it)
├── psycopg[binary] (Postgres driver)
├── httpx           (async HTTP)
├── arq             (job queue, simpler than Celery for solo)
├── rasterio        (Phase 4 — SAR)
├── shapely + pyproj (geometry, projections)
├── pytest          (tests)
└── ruff + mypy     (linting + type-checking)

Frontend
├── Vite + TypeScript
├── React 18
├── @tanstack/react-query  (server state)
├── MapLibre GL JS         (real maps, free, no Mapbox token)
├── deck.gl                (data layers — Phase 4+)
├── @clerk/clerk-react     (auth)
├── lucide-react           (icons)
└── @radix-ui              (accessible primitives)

Data sources (Phase 2+)
├── AISStream.io       (free, real-time AIS WebSocket)
├── Copernicus Data Space (free Sentinel-1/2)
├── NASA FIRMS         (free VIIRS thermal — wildfire path)
├── NOAA APIs          (weather, water, sea state)
└── USGS Water         (free stream gauges — flood path)

Hosting
├── Fly.io             (backend + Postgres)
├── Vercel             (frontend)
├── Cloudflare         (DNS, CDN)
└── Tigris/R2          (object storage for SAR scenes — Phase 4)
```

Estimated total monthly cost across all 6 months: **$15–40**
depending on data volume.

---

## Skills you'll level up — and the order they matter

You said "no edge yet." The plan above forces you to build edges
in the order you'll need them.

| Phase | Skill you'll develop | Why it matters |
|-------|---------------------|----------------|
| 0–1 | Deployment, DB design, system thinking | Foundation. Without this you can't ship anything else. |
| 2–3 | Async data pipelines, real-data debugging | The heart of the product. |
| 4 | Geospatial / remote sensing | This is the *durable* edge. SAR is genuinely rare expertise. |
| 5 | Product writing, user research, distribution | The most undervalued skills in tech. |
| 6 | Domain depth (whichever fork) | Where you become an expert worth talking to. |

**Specific learning resources, in order of when you'll need them:**

1. [Full Stack FastAPI Template](https://github.com/fastapi/full-stack-fastapi-template) — clone, read, learn the patterns.
2. [Crunchy Data PostGIS tutorials](https://www.crunchydata.com/developers/tutorials) — best PostGIS resource.
3. [TanStack Query Essentials](https://query.gg/) (paid but worth it).
4. [Awesome SAR](https://github.com/RadarCODE/awesome-sar) — the community reading list.
5. [The Mom Test](https://www.momtestbook.com/) — book on talking to users without leading them. Read before Phase 5 calls.
6. [Working in Public](https://press.stripe.com/working-in-public) — Nadia Asparouhova on running an open project sustainably.

---

## Hard truths

These need to be said so you don't burn out.

**Most weeks, you'll do less than you planned.** That's normal.
Build a 6-month plan, expect it to take 9. Celebrate the months
where you ship anything at all.

**Real data is humbling.** You will spend 3× more time on data
quality issues in Phase 2 than you expect. AIS messages have
fields you've never heard of. Vessels appear in lakes. Timestamps
arrive out of order. This isn't a sign you're doing it wrong —
it's the actual job.

**Solo evenings-and-weekends has a 6-month half-life.** Energy
fades. Plan for that. Phase 6's "pick your fork" exists partly so
that you have a fresh challenge if you're flagging, and partly so
you have an off-ramp if you're not.

**You are competing with Cursor, Claude Code, and AI coding tools
that change the game weekly.** Use them. The plan above is much
faster with an AI pair than without one. Don't romanticize doing
it the hard way.

**The flood domain you're excited about is harder than maritime,
not easier.** Don't start there. Use the maritime ship as the
proof that your architecture works, then port it. If you start
with flood you'll be debugging the architecture and the domain
simultaneously, and you'll quit.

**Talking to potential users is the highest-leverage activity.**
Most engineers, including me-when-I-was-younger, treat user
conversations as an interruption. They're not. One 30-min call
with a Coast Guard analyst will reorient your roadmap more than
two weekends of coding. Schedule them aggressively starting in
Phase 5.

---

## What to do this weekend

Start Phase 0 right now:

1. Push the MVP to a public GitHub repo with a real README.
2. Buy the domain.
3. Sign up for Fly.io and Vercel.
4. Pick a focus AOI (suggestion: somewhere within 200 nm of
   a coast you find interesting — gives you both fishing and
   transit traffic).

That's a 2–3 hour session. If you finish it, the rest of the
plan has a foothold. If you don't, the plan was too ambitious for
your actual time budget — and that's important to know now, not
in Month 4.

You're going to do this in fits and starts. That's fine. The
architecture you have is good. The hard part isn't building it —
it's not abandoning it. Plan accordingly.
