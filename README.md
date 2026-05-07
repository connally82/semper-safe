# Semper Safe

> Civilian sensor fusion. Always safe, always auditable.

A civilian-first sensor-fusion platform — the inverse of military targeting
systems. Same architecture (heterogeneous sensor ingestion → entity resolution
→ recommendation → human-in-the-loop dispatch), oriented entirely toward
**rescue, alert, and dispatch** instead of strike. Built around a tamper-evident
audit log so an oversight body can verify every operational decision.

**Status:** prototype. Multi-domain MVP runs on synthetic data. Production
deployment is a multi-month roadmap (see [`docs/roadmap.md`](docs/roadmap.md)).

**Built by:** [@connally82](https://github.com/connally82) — solo, evenings & weekends.

---

## What's working today

| Phase | Domain | Status |
|------|--------|--------|
| **01** | Maritime SAR + dark-vessel detection | ✅ MVP runs end-to-end on synthetic data |
| **02** | Wildfire early detection (thermal + smoke + weather) | ✅ MVP runs end-to-end on synthetic data |
| 03 | Anti-poaching | future |
| 04 | Wilderness SAR (lost-person tracking) | future |
| 05 | AMBER, humanitarian, flood | future (full guardrail stack required first) |

The phasing is not arbitrary: each domain that touches *people directly* is
gated on years of clean operational evidence in lower-stakes domains first.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Sensor mesh (synthetic for MVP)                                │
│  ─ AIS    ─ SAR    ─ VIIRS/GOES    ─ Optical    ─ Weather       │
└────────────────┬────────────────────────────────────────────────┘
                 │
       ┌─────────▼──────────┐
       │  Ingestion         │   Normalize → (cell, time, source, conf)
       └─────────┬──────────┘
                 │
       ┌─────────▼──────────┐
       │  Per-domain fusion │   Maritime engine + Wildfire engine
       │  (pluggable)       │   sharing a common Entity model
       └─────────┬──────────┘
                 │
       ┌─────────▼──────────┐
       │  Workbench API     │   FastAPI · multi-domain
       │  + Audit log       │   Hash-chained, append-only
       └─────────┬──────────┘
                 │
       ┌─────────▼──────────┐
       │  React workbench   │   Map, lineage, approve/reject
       └────────────────────┘
```

The same core handles different domains via plugins. Adding wildfire to the
maritime baseline required ~600 lines of new code and zero changes to the
audit log, entity model, or workbench shell.

---

## Quick start

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

API documentation auto-generated at `http://localhost:8000/docs`.

The frontend workbench (`frontend/Workbench.jsx`) runs against the live API
or falls back to embedded scenario data. A proper Vite scaffold lands in Phase 0
of the roadmap.

### What the demo shows

**Maritime:** A 6-hour synthetic scenario off NW Madagascar with 12
cooperative vessels broadcasting AIS, one legitimate AIS dropout
(self-declared maintenance), and two vessels going dark near a marine
protected area. Four SAR satellite passes detect the dark vessels. The
fusion engine distinguishes routine dropouts from suspicious ones via
context, and the workbench shows full lineage from raw observation to
recommended action.

**Wildfire:** A 2-hour synthetic scenario across Northern California with
a confirmed WUI fire (red flag conditions → evacuation advisory), a
remote fire (alert dispatch only), an industrial false positive (refinery
flare, suppressed before reaching the operator), an isolated single-pixel
hotspot (watchlist), and an orphan smoke plume.

---

## Repo layout

```
semper-safe/
├── README.md
├── LICENSE
├── .gitignore
├── docs/
│   ├── blueprint.md         # full architectural blueprint
│   └── roadmap.md           # 6-month solo build-out plan
├── backend/
│   ├── models.py            # shared types — Observation, Entity, AuditEntry
│   ├── audit.py             # hash-chained append-only log
│   ├── fusion.py            # Phase 1: maritime engine
│   ├── seed_data.py         # Phase 1: synthetic AIS+SAR scenario
│   ├── wildfire.py          # Phase 2: wildfire engine (the plugin)
│   ├── wildfire_seed.py     # Phase 2: synthetic VIIRS+GOES scenario
│   ├── main.py              # FastAPI multi-domain app
│   └── requirements.txt
└── frontend/
    └── Workbench.jsx        # multi-domain React workbench
```

---

## Design principles

These are load-bearing — they're what makes this *civilian* infrastructure
rather than a re-skinned military system:

1. **Lineage over output.** Every recommendation traces back to source
   observations. No black box. The lineage panel is the inspectability
   surface for an oversight body.
2. **Append-only audit.** Every state change is hash-chained. Tampering
   invalidates the chain from that point forward.
3. **Human-in-the-loop, named accountability.** No autonomous dispatch.
   Ever. Every action carries the operator's name into the audit log.
4. **Phased deployment as moral constraint.** Lower-stakes domains
   (open-ocean maritime) before higher-stakes domains (wilderness SAR
   with biometrics, AMBER). The phase order is not just engineering —
   it's how the platform earns the right to track people.
5. **False-positive suppression as first-class.** Civilian platforms
   live or die on alarm fatigue. Known industrial flares, known shipping
   lanes, known controlled burns get suppressed *before* reaching the
   operator queue, with explicit audit entries.
6. **Civilian inspectability over secrecy.** The architecture is open.
   The audit log is verifiable. There is no classified path.

---

## Roadmap

See [`docs/roadmap.md`](docs/roadmap.md) for the 6-month solo plan. High level:

- **Months 1–2:** Deploy current MVP. Migrate to PostgreSQL.
- **Months 2–3:** Real AIS data via [AISStream.io](https://aisstream.io). Live
  dark-vessel detection in one area of operation.
- **Months 3–4:** Auth + email alerts. First user research calls.
- **Months 4–5:** Real Sentinel-1 SAR fusion (CFAR vessel detection).
- **Month 6:** Pick a fork — wildfire on real data, flood, or deepen maritime.

---

## Why this exists

The same heterogeneous-sensor architecture that compresses a military
"observe-orient-decide-act" loop to *strike faster* can compress it to
*rescue faster, alert sooner, dispatch smarter* — with the auditability
and oversight that military systems explicitly avoid.

Building the civilian version isn't about taking the military version and
removing the bad parts. It's about inverting the orientation from the
ground up: lineage as a feature instead of a leak, false-positive
suppression as user respect instead of capability loss, phased deployment
as social license instead of engineering sequencing.

---

## License

MIT — see [`LICENSE`](LICENSE).

The MIT license is deliberate. Civilian safety infrastructure should be
forkable, inspectable, and reusable. If a coastal nation, a national park
service, or a fire district wants to deploy this, they shouldn't need to
ask permission.

---

## Contact

Issues and PRs welcome. For research collaboration, partnership, or
questions about the architecture, open an issue on this repo.
