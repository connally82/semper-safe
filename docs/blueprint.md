# Semper Safe — Architectural Blueprint
### A civilian sensor fusion platform — adapted from the Maven blueprint

> Civilian analog to Palantir's Maven Smart System. Same OODA-loop architecture, repurposed for **finding people, assets, and events in the physical world and dispatching response** — never targeting. The "act" step is rescue, alert, or dispatch.

---

## 1. Unified problem statement

Across the target use cases — Search & Rescue, AMBER / missing persons, wildfire early detection, illegal fishing, anti-poaching, humanitarian logistics — the operational pattern is identical:

1. **Many heterogeneous sensors** cover an area of interest
2. **An entity to find** — lost hiker, missing child, smoke plume, dark vessel, poacher, displaced family
3. **A response endpoint** — rescue team, law enforcement, fire crew, coast guard, ranger, aid convoy
4. **Time pressure** — minutes translate to lives

Different domains, same plumbing. Build the plumbing once, then ship domain plugins.

---

## 2. Architecture — five layers

### Layer 1: Sensor mesh

**Always-on**
- **Earth observation**: Planet (daily 3m optical), Capella + ICEYE (SAR, all-weather, day/night), Sentinel-1/2 (free baseline)
- **RF / signals**: Spire (vessel + aircraft RF), HawkEye 360 (geolocated emitters)
- **Self-reporting transponders**: AIS (ships), ADS-B (aircraft), APRS (amateur radio — used by hikers/sailors)
- **Weather & environment**: NOAA, ECMWF, lightning networks, USGS seismic

**On-demand / aerial**
- Autonomous drone docks (DJI Dock, Skydio Dock) for routine patrol
- Manned aircraft EO/IR for SAR overflight
- Tethered aerostats for persistent wide-area coverage during disasters

**Ground / community**
- 911 / 112 / NCMEC streams (with appropriate authority)
- Citizen reports — geotagged, Waze-style
- Crowdsourced Bluetooth (Tile, AirTag) — **opt-in only**
- Cellular triangulation — warrant-bound, AMBER-only
- Hydrophone arrays (marine mammal strike avoidance, tsunami)
- Wildfire camera networks (ALERTCalifornia model)

**Domain-specific**
- VIIRS / GOES thermal hotspots (wildfire)
- Acoustic gunshot detection (anti-poaching)
- License plate readers — AMBER only, audited, sunset-bound

### Layer 2: Ingest & normalization

- **Spatiotemporal grid**: H3 hex cells (Uber, resolution 8–12 depending on use) + UTC microsecond timestamps. Every observation lands as a `(cell, time, source, confidence)` tuple.
- **Universal Entity Schema**: `Entity { id, type, geometry, time_window, attributes{}, source_lineage[], confidence }`
- **Stream backbone**: Kafka or Apache Pulsar. Hot path measured in seconds.
- **Provenance, non-negotiable**: every assertion traces to source observations. No claim without lineage. This is the line between civilian (auditable) and military (black-box).

### Layer 3: Detection & segmentation

**Backbone**: SAM 3 / Grounding DINO / CLIP for zero-shot segmentation and labeling.

**Per-domain finetuned heads**:
| Domain | Detection head |
|---|---|
| Wilderness SAR | Small-target person detection in cluttered terrain (synthetic + real survivor-pose data) |
| AMBER | Vehicle re-identification — make/model/color/distinctive features across cameras |
| Illegal fishing | Dark-vessel SAR detection + AIS-gap correlation |
| Wildfire | Smoke / thermal anomaly classifier on geostationary feeds |
| Anti-poaching | Animal + human detection in camera traps + thermal drone; behavioral flags |
| Humanitarian | Population-displacement change detection on settlement footprints |

LoRA finetunes on top of SAM3/DINO let you ship new domain heads in weeks, not quarters.

### Layer 4: Fusion & entity resolution

Three sub-systems — this is the core IP:

1. **Multi-modal entity linking**: Bayesian matching across sources. *"SAR detection at (x, y, t) + AIS gap at nearby (x′, y′, t′) + Spire RF emitter ⇒ single Entity, P = 0.84."*
2. **Track fusion**: Kalman / particle / IMM ensemble for moving targets across sensors with different noise profiles.
3. **Pattern-of-life**: per-area normal baseline (vessel density, hiker traffic, fire weather indices). Anomalies surface for human review.

**Output**: ranked watch-list of Entities with confidence, recommended next observations, and suggested response actions. **Never autonomous.**

### Layer 5: Decision support / workbench

Map-centric, time-scrubbable, layered UI (MapLibre + Deck.gl).

- Suggested actions: *"task SAR sat for 14:32 pass," "dispatch drone X to grid Y," "issue AMBER alert for vehicle Z," "alert coast guard sector A."*
- Every suggestion shows its evidence chain — one click expands the lineage view.
- Human approves or rejects with a reason code.
- **Dispatch integrations**: CAD (Computer-Aided Dispatch) for police/fire, NCMEC for AMBER, IFRC for humanitarian, AFRCC for SAR.
- **Full audit log**: every observation, fusion call, recommendation, decision, dispatch — append-only with cryptographic chaining.

---

## 3. Cross-domain modularity

**Core (build once)**: ingestion, entity schema, fusion engine, workbench, audit, dispatch interfaces.

**Domain plugins (build per use case)**: sensor adapters, detection model heads, dispatch endpoints, response playbooks.

A lost hiker and a missing child are both `Entity{type: person, status: missing}`. The platform doesn't care; the playbook does.

---

## 4. Guardrails — what makes this NOT military

These aren't bolt-ons. They're load-bearing architectural choices.

1. **Data minimization** — ingest only what serves the active mission.
2. **Purpose limitation** — SAR data cannot be repurposed for immigration enforcement, ad targeting, or anything else. Enforced via separate encryption domains per purpose.
3. **Sunset rules** — incident closes, data purges on a schedule. ~30 days for closed SAR; longer for unsolved AMBER; case-bound for poaching evidence pending prosecution.
4. **Independent oversight board** — civilian members, subpoena-grade audit access, public quarterly reports.
5. **Public transparency** — open architecture, redacted incident summaries, model cards for every detection head.
6. **Civil-liberties firewall** — any module tracking people (AMBER, SAR with biometrics) requires per-incident legal authorization, not blanket access.
7. **Human-in-the-loop, named accountability** — every "act" has a person's name on it. No autonomous dispatch. Ever.
8. **Red team & adversarial testing** — published bug bounty, robustness audits, fairness audits per domain.

---

## 5. Phased roadmap

Build order isn't about difficulty. It's about **earning the right** to touch people-tracking by operating cleanly on lower-stakes domains first.

| Phase | Timeline | Domain | Why this order |
|---|---|---|---|
| 1 | Months 0–6 | **Maritime SAR + dark-vessel detection** | Lowest privacy stakes (open ocean, AIS is public). Cleanest sensor mix. Validates the fusion engine. |
| 2 | Months 6–12 | **Wildfire early detection** | Land-based, non-personal. Adds environmental modeling. |
| 3 | Months 12–18 | **Anti-poaching** | Wilderness, low population density, narrow legal context. First time tracking adversarial humans. |
| 4 | Months 18–24 | **Wilderness SAR** | Now tracking lost people — consent and minimization stack must be live. |
| 5 | Months 24–36 | **Humanitarian logistics + AMBER** | Highest stakes (vulnerable populations, missing children). Full guardrail stack and oversight board mature. |

Doing AMBER in Phase 1 would be reckless. The phasing is the social license.

---

## 6. Tech stack — concrete picks

- **Geo**: H3 (Uber) or S2 (Google) for indexing; PostGIS for relational; GDAL for raster.
- **Streaming**: Kafka + Apache Beam, or Pulsar.
- **ML**: PyTorch; SAM 3 + Grounding DINO + CLIP backbones; LoRA for domain finetunes; Triton for serving.
- **Vector DB** (entity embeddings): Qdrant or Weaviate.
- **Workbench**: React + MapLibre GL + Deck.gl; Three.js for 3D where useful.
- **Backend**: Python (FastAPI) for API and glue; Rust for hot paths (track fusion, entity resolution).
- **Audit**: append-only event log with Merkle chaining, externally anchored daily (e.g., to a public timestamp authority).
- **Cloud**: agnostic — AWS, GCP, Azure, or on-prem. **Specifically NOT classified clouds.** Civilian platforms must be inspectable by oversight.

---

## 7. The 90-day MVP — solo or small team

Maritime SAR / dark-vessel detection demo:

1. **Data**: pull Sentinel-1 SAR (free), AIS (free via AISHub or Spire trial), weather from NOAA.
2. **Pipeline**: H3 grid + Postgres + Kafka ingestion.
3. **Detection**: SAM 3 vessel segmentation on SAR + AIS-gap correlation logic.
4. **Workbench**: React + MapLibre; layers for SAR detections, AIS tracks, transponder gaps, suggested tasking.
5. **Dispatch**: synthetic endpoint (a logged "alert" — no real coast guard integration yet).

This validates the entire architecture on a low-stakes domain before extending to anything that touches people. If the maritime MVP can't reliably correlate a SAR blob to an AIS gap and surface it with a clean lineage view, none of the harder domains are ready.

---

## 8. What this deliberately is *not*

- Not real-time autonomous action — every "act" requires a named human approval.
- Not a black box — every output traces to source observations.
- Not a single-customer platform — the same core serves coast guards, fire agencies, and aid orgs.
- Not classified — civilian inspectability is a feature, not a constraint.
- Not pointed at people first — Phase 1 is open ocean, on purpose.

The Maven architecture is genuinely powerful. The blueprint above keeps the power and inverts the orientation: instead of compressing the OODA loop to strike faster, it compresses it to **rescue faster, alert sooner, dispatch smarter** — with auditability and oversight that military systems explicitly avoid.
