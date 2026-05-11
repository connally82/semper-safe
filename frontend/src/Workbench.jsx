import React, { useState, useMemo, useRef, useEffect, useCallback } from "react";
import MapLibreView from "./MapLibreView.jsx";

// =============================================================
//   SEMPER SAFE — Multi-Domain Workbench
//   Phase 1 (Maritime SAR) + Phase 2 (Wildfire) on a shared core.
//
//   Behavior:
//   - On mount, tries to reach the backend at API_BASE.
//   - If reachable, uses live data (entities, lineage, audit, decisions).
//   - If not, falls back to embedded scenario data — same shape as
//     the API would return. Decisions stay client-side in offline mode.
// =============================================================

// Configurable per environment via Vite's import.meta.env.
//   dev:  VITE_API_BASE=http://localhost:8000  (frontend/.env.local)
//   prod: VITE_API_BASE=https://semper-safe.fly.dev  (Vercel env)
const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// =============================================================
//   Embedded scenario data (offline fallback)
// =============================================================
const MARITIME = {"audit_head":"eb40f2b5fe308429312ef1c862b4bdb5ada2cf31fc0b0b55896c3c3959f171b0","audit_count":1749,"entities":[{"id":"ent_0200cd72b5","type":"dark_vessel","lon":48.117,"lat":-13.5699,"priority":0.95,"confidence":0.82,"first_seen":"2026-05-07T09:00:00+00:00","last_seen":"2026-05-07T10:30:00+00:00","name":null,"obs_count":3,"track":[[48.0657,-13.4898],[48.0914,-13.5312],[48.117,-13.5699]],"recommendation":{"action":"task_sar_satellite","rationale":"SAR detection unmatched to any cooperative AIS target. Recommend tasking next satellite pass for confirmation before alerting surface assets.","rec_id":"rec_c371791526"}},{"id":"ent_e9e2c7a5ec","type":"dark_vessel","lon":48.2743,"lat":-13.859,"priority":0.95,"confidence":0.82,"first_seen":"2026-05-07T09:45:00+00:00","last_seen":"2026-05-07T11:15:00+00:00","name":null,"obs_count":3,"track":[[48.3328,-13.9985],[48.3118,-13.9547],[48.2743,-13.859]],"recommendation":{"action":"task_sar_satellite","rationale":"SAR detection unmatched to any cooperative AIS target. Recommend tasking next satellite pass for confirmation before alerting surface assets.","rec_id":"rec_2624a59e26"}},{"id":"ent_1c056c6414","type":"dark_vessel","lon":48.1704,"lat":-13.6566,"priority":0.9,"confidence":0.77,"first_seen":"2026-05-07T10:30:00+00:00","last_seen":"2026-05-07T11:15:00+00:00","name":null,"obs_count":2,"track":[[48.1471,-13.6113],[48.1704,-13.6566]],"recommendation":{"action":"task_sar_satellite","rationale":"SAR detection unmatched to any cooperative AIS target. Recommend tasking next satellite pass for confirmation before alerting surface assets.","rec_id":"rec_6b996e5989"}},{"id":"ent_9e1552e289","type":"dark_vessel","lon":48.0372,"lat":-13.4429,"priority":0.85,"confidence":0.72,"first_seen":"2026-05-07T09:00:00+00:00","last_seen":"2026-05-07T09:00:00+00:00","name":null,"obs_count":1,"track":[[48.0372,-13.4429]],"recommendation":{"action":"task_sar_satellite","rationale":"SAR detection unmatched to any cooperative AIS target. Recommend tasking next satellite pass for confirmation before alerting surface assets.","rec_id":"rec_7a629c0cc2"}},{"id":"ent_a18b13e5c0","type":"ais_gap","lon":48.5667,"lat":-14.3,"priority":0.55,"confidence":0.99,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T08:00:00+00:00","name":"MV CORAL VOYAGER","mmsi":"311234567","vtype":"cargo","notes":"AIS dropout. Self-declared port maintenance.","obs_count":41,"track":[[48.9,-14.3],[48.85,-14.3],[48.8,-14.3],[48.75,-14.3],[48.7,-14.3],[48.65,-14.3],[48.6,-14.3],[48.5667,-14.3]],"recommendation":{"action":"log_only","rationale":"AIS dropout exceeds threshold but vessel self-declared port maintenance prior. Watch for resumption. No surface dispatch.","rec_id":"rec_5a6a491b1e"}},{"id":"ent_a66328f21b","type":"ais_gap","lon":48.0241,"lat":-13.4241,"priority":0.55,"confidence":0.99,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T08:30:00+00:00","name":"FV SEA TIGER","mmsi":"412345678","vtype":"fishing","notes":"AIS dropout. No prior maintenance flag.","obs_count":51,"track":[[47.7,-13.1],[47.7648,-13.1648],[47.8296,-13.2296],[47.8945,-13.2945],[47.9463,-13.3463],[48.0241,-13.4241]],"recommendation":{"action":"log_only","rationale":"AIS dropout exceeds threshold. Watch for resumption or correlate with next SAR pass. No surface dispatch without corroboration.","rec_id":"rec_acb09c4960"}},{"id":"ent_e5bfc757d6","type":"ais_gap","lon":48.3659,"lat":-14.0659,"priority":0.55,"confidence":0.99,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T09:09:00+00:00","name":"FV OCEAN HARVEST","mmsi":"413456789","vtype":"fishing","notes":"AIS dropout. No prior maintenance flag.","obs_count":65,"track":[[48.7,-14.4],[48.6205,-14.3205],[48.5409,-14.2409],[48.4614,-14.1614],[48.3977,-14.0977],[48.3659,-14.0659]],"recommendation":{"action":"log_only","rationale":"AIS dropout exceeds threshold. Watch for resumption or correlate with next SAR pass. No surface dispatch without corroboration.","rec_id":"rec_3cd7f3aaf5"}},{"id":"ent_v01","type":"vessel","lon":48.6398,"lat":-14.8507,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV ALBATROSS","mmsi":"313645529","vtype":"cargo","obs_count":125,"track":[[48.5004,-14.005],[48.5283,-14.1741],[48.5562,-14.3433],[48.5829,-14.5054],[48.6084,-14.6604],[48.6398,-14.8507]]},{"id":"ent_v02","type":"vessel","lon":48.3987,"lat":-13.4495,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV CALYPSO","mmsi":"394253183","vtype":"cargo","obs_count":125,"track":[[47.9802,-14.2575],[48.0639,-14.0959],[48.1476,-13.9343],[48.2278,-13.7795],[48.3045,-13.6313],[48.3987,-13.4495]]},{"id":"ent_v03","type":"vessel","lon":48.2614,"lat":-12.6765,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV PETREL","mmsi":"356987307","vtype":"cargo","obs_count":125,"track":[[48.4353,-13.6385],[48.4005,-13.4461],[48.3657,-13.2537],[48.3324,-13.0693],[48.3005,-12.8929],[48.2614,-12.6765]]},{"id":"ent_v04","type":"vessel","lon":47.5992,"lat":-13.1843,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV SEA PIONEER","mmsi":"383495800","vtype":"cargo","obs_count":125,"track":[[48.6495,-13.4785],[48.4395,-13.4197],[48.2294,-13.3608],[48.0281,-13.3044],[47.8356,-13.2505],[47.5992,-13.1843]]},{"id":"ent_v05","type":"vessel","lon":48.3518,"lat":-14.7105,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV NORDIC SUN","mmsi":"363695976","vtype":"cargo","obs_count":125,"track":[[47.5184,-13.8157],[47.6851,-13.9946],[47.8517,-14.1736],[48.0115,-14.3451],[48.1643,-14.5092],[48.3518,-14.7105]]},{"id":"ent_v06","type":"vessel","lon":48.2497,"lat":-13.3341,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV TRADEWIND","mmsi":"357346124","vtype":"cargo","obs_count":125,"track":[[48.7394,-14.1549],[48.6414,-13.9907],[48.5435,-13.8266],[48.4497,-13.6693],[48.3599,-13.5188],[48.2497,-13.3341]]},{"id":"ent_v07","type":"vessel","lon":48.23,"lat":-12.7217,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV KESTREL","mmsi":"381712030","vtype":"cargo","obs_count":125,"track":[[47.7076,-13.4047],[47.8121,-13.2681],[47.9166,-13.1315],[48.0167,-13.0006],[48.1125,-12.8754],[48.23,-12.7217]]},{"id":"ent_v08","type":"vessel","lon":48.2015,"lat":-12.3932,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV HORIZON","mmsi":"395862298","vtype":"cargo","obs_count":125,"track":[[48.0032,-13.3591],[48.0429,-13.1659],[48.0825,-12.9727],[48.1206,-12.7876],[48.1569,-12.6105],[48.2015,-12.3932]]},{"id":"ent_v09","type":"vessel","lon":47.0617,"lat":-14.5394,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV TRITON","mmsi":"362577364","vtype":"cargo","obs_count":125,"track":[[48.3485,-14.4526],[48.0712,-14.4699],[47.7938,-14.4873],[47.5280,-14.5040],[47.2737,-14.5199],[47.0617,-14.5394]]},{"id":"ent_v10","type":"vessel","lon":47.9491,"lat":-13.1001,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV MARLIN","mmsi":"387345125","vtype":"cargo","obs_count":125,"track":[[48.7427,-13.7081],[48.584,-13.5865],[48.4253,-13.4649],[48.2732,-13.3483],[48.1277,-13.2369],[47.9491,-13.1001]]},{"id":"ent_v11","type":"vessel","lon":48.4839,"lat":-15.0326,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV BLUEFIN","mmsi":"384357300","vtype":"cargo","obs_count":125,"track":[[47.8562,-13.7868],[47.9817,-14.0360],[48.1073,-14.2851],[48.2276,-14.5239],[48.3426,-14.7523],[48.4839,-15.0326]]},{"id":"ent_v12","type":"vessel","lon":48.2351,"lat":-14.1525,"priority":0.05,"confidence":1.0,"first_seen":"2026-05-07T06:00:00+00:00","last_seen":"2026-05-07T12:00:00+00:00","name":"MV SOUTHERN CROSS","mmsi":"393624666","vtype":"cargo","obs_count":125,"track":[[47.7514,-13.0517],[47.8481,-13.2719],[47.9449,-13.4920],[48.0376,-13.7030],[48.1263,-13.9048],[48.2351,-14.1525]]}]};

const WILDFIRE = {"audit_head":"7c8a91f3edaa11e5d72f4193e0ba33891f2d05c1ec4732e95da2880e5a91f3ed","audit_count":42,"entities":[{"id":"fire_b64585e0da","type":"fire_event","lon":-122.607,"lat":38.4774,"priority":0.93,"confidence":0.97,"first_seen":"2026-09-18T14:00:00+00:00","last_seen":"2026-09-18T15:15:00+00:00","name":"Santa Rosa fire (WUI)","notes":"Persistent thermal anomaly across multiple detections. RED FLAG conditions: low RH + high wind + critical fuels.","obs_count":8,"obs":[{"lon":-122.61,"lat":38.475,"src":"viirs"},{"lon":-122.609,"lat":38.4758,"src":"viirs"},{"lon":-122.608,"lat":38.476,"src":"goes"},{"lon":-122.595,"lat":38.487,"src":"optical"},{"lon":-122.608,"lat":38.4766,"src":"viirs"},{"lon":-122.608,"lat":38.476,"src":"goes"},{"lon":-122.607,"lat":38.4774,"src":"viirs"},{"lon":-122.61,"lat":38.475,"src":"weather"}],"attrs":{"frp_mw":14,"weather":{"rh_pct":12,"wind_mph":31,"wind_gust_mph":48,"temp_f":92,"fuel_moisture":6,"advisory":"RED FLAG WARNING"}},"recommendation":{"action":"evacuation_advisory","rationale":"Confirmed fire event 1.5 km from Santa Rosa WUI. Recommend evacuation advisory for affected zones. REQUIRES named fire-officer approval before issuance.","rec_id":"rec_6b3c6a02c6"}},{"id":"fire_9368654883","type":"fire_event","lon":-123.1776,"lat":40.6082,"priority":0.85,"confidence":0.94,"first_seen":"2026-09-18T14:10:00+00:00","last_seen":"2026-09-18T15:50:00+00:00","name":"Trinity NF fire","notes":"Persistent thermal anomaly across multiple detections. Remote — no WUI within 50 km.","obs_count":6,"obs":[{"lon":-123.18,"lat":40.61,"src":"viirs"},{"lon":-123.1792,"lat":40.6094,"src":"viirs"},{"lon":-123.1784,"lat":40.6088,"src":"viirs"},{"lon":-123.172,"lat":40.605,"src":"optical"},{"lon":-123.18,"lat":40.61,"src":"weather"},{"lon":-123.1776,"lat":40.6082,"src":"viirs"}],"attrs":{"frp_mw":8,"weather":{"rh_pct":28,"wind_mph":12,"fuel_moisture":11,"temp_f":78}},"recommendation":{"action":"alert_fire_dispatch","rationale":"Confirmed fire event. Recommend dispatching ground and aerial assets per local response plan.","rec_id":"rec_2049567311"}},{"id":"plume_1a0a3ad4f9","type":"smoke_plume","lon":-119.84,"lat":36.72,"priority":0.4,"confidence":0.6,"first_seen":"2026-09-18T15:10:00+00:00","last_seen":"2026-09-18T15:10:00+00:00","name":null,"notes":"Smoke plume detected without matching thermal anomaly. Possibly distant fire, controlled burn, or dust.","obs_count":1,"obs":[{"lon":-119.84,"lat":36.72,"src":"optical"}],"attrs":{},"recommendation":null},{"id":"fire_9a28a9ae31","type":"hotspot","lon":-120.42,"lat":39.18,"priority":0.3,"confidence":0.55,"first_seen":"2026-09-18T14:30:00+00:00","last_seen":"2026-09-18T14:30:00+00:00","name":null,"notes":"Single thermal detection. Awaiting persistence or smoke corroboration before escalation.","obs_count":1,"obs":[{"lon":-120.42,"lat":39.18,"src":"viirs"}],"attrs":{"frp_mw":4},"recommendation":null},{"id":"fp_d08dcd77","type":"false_positive","lon":-121.89,"lat":38.02,"priority":0.0,"confidence":0.99,"first_seen":"2026-09-18T14:55:00+00:00","last_seen":"2026-09-18T14:55:00+00:00","name":"Martinez refinery","notes":"Suppressed: known thermal source (Martinez refinery flare stack).","obs_count":2,"obs":[{"lon":-121.89,"lat":38.02,"src":"viirs"},{"lon":-121.889,"lat":38.021,"src":"goes"}],"attrs":{"suppression_reason":"Martinez refinery flare stack"},"recommendation":null}]};

const INITIAL_AUDIT = {
  maritime: [
    { seq: 1, t: "06:00:00", actor: "system", event: "domain_loaded", note: "maritime · 1666 obs queued" },
    { seq: 247, t: "08:00:12", actor: "system", event: "entity_reclassified", note: "MV CORAL VOYAGER → ais_gap" },
    { seq: 248, t: "08:00:12", actor: "system", event: "recommendation_made", note: "log_only (self-declared maintenance)" },
    { seq: 412, t: "08:30:08", actor: "system", event: "entity_reclassified", note: "FV SEA TIGER → ais_gap" },
    { seq: 731, t: "09:00:01", actor: "system", event: "entity_created", note: "ent_9e1552e289 → dark_vessel (capella_pass_001)" },
    { seq: 732, t: "09:00:01", actor: "system", event: "recommendation_made", note: "task_sar_satellite (priority 0.85)" },
    { seq: 894, t: "09:09:14", actor: "system", event: "entity_reclassified", note: "FV OCEAN HARVEST → ais_gap" },
    { seq: 1102, t: "09:45:02", actor: "system", event: "observation_associated", note: "sar_track_continuity → ent_e9e2c7a5ec" },
    { seq: 1340, t: "10:30:01", actor: "system", event: "observation_associated", note: "sar_track_continuity → ent_0200cd72b5" },
    { seq: 1341, t: "10:30:01", actor: "system", event: "priority_increased", note: "ent_0200cd72b5 0.90 → 0.95" },
    { seq: 1601, t: "11:15:03", actor: "system", event: "observation_associated", note: "sar_track_continuity → ent_e9e2c7a5ec" },
  ],
  wildfire: [
    { seq: 1750, t: "14:00:00", actor: "system", event: "domain_loaded", note: "wildfire" },
    { seq: 1755, t: "14:00:00", actor: "system", event: "entity_created", note: "fire_b64585e0da → hotspot (viirs)" },
    { seq: 1761, t: "14:25:00", actor: "system", event: "entity_reclassified", note: "fire_b64585e0da → fire_event (thermal_persistence)" },
    { seq: 1762, t: "14:25:00", actor: "system", event: "recommendation_made", note: "alert_fire_dispatch" },
    { seq: 1769, t: "14:30:00", actor: "system", event: "entity_created", note: "fire_9a28a9ae31 → hotspot (single pixel, low FRP)" },
    { seq: 1772, t: "14:45:00", actor: "system", event: "observation_associated", note: "smoke_to_thermal → fire_b64585e0da" },
    { seq: 1773, t: "14:45:00", actor: "system", event: "entity_reclassified", note: "fire_b64585e0da → fire_event (smoke_corroboration)" },
    { seq: 1781, t: "14:55:00", actor: "system", event: "false_positive_suppressed", note: "Martinez refinery flare stack" },
    { seq: 1786, t: "15:00:00", actor: "system", event: "false_positive_suppressed", note: "Martinez refinery flare stack" },
    { seq: 1791, t: "15:10:00", actor: "system", event: "entity_created", note: "plume_1a0a3ad4f9 → smoke_plume (orphan)" },
    { seq: 1798, t: "15:20:00", actor: "system", event: "priority_increased", note: "fire_b64585e0da 0.85 → 0.93 (red_flag_conditions)" },
    { seq: 1799, t: "15:20:00", actor: "system", event: "recommendation_updated", note: "→ evacuation_advisory (1.5km from WUI)" },
  ],
};

// =============================================================
//   Domain config
// =============================================================
const DOMAINS = {
  maritime: {
    label: "MARITIME · PHASE 01",
    sub: "SAR & dark-vessel detection",
    apiPath: "/maritime",
    accent: "#5dd6c4",
    operator: "lt.morrison@noaa",
    operatorRole: "Maritime watch officer",
    polygons: [
      { type: "mpa", lon: 48.2, lat: -13.7, radius: 0.25, label: "MARINE PROTECTED AREA",
        color: "#f0a830", aspect: 0.8 },
    ],
    typeMeta: {
      vessel:           { color: "#6b8db8", label: "VESSEL",           glow: false },
      ais_gap:          { color: "#f0a830", label: "AIS GAP",          glow: true },
      dark_vessel:      { color: "#ff5c5c", label: "DARK VESSEL",      glow: true },
      // Distinct purple — not red (that's "dark vessel"), not orange
      // (that's "AIS gap"), not blue (that's "underway"). Glow on so
      // the eye picks it out of a dense vessel cluster at port approach.
      loitering_vessel: { color: "#c08bdc", label: "LOITERING",        glow: true },
      // Cyan-pink for SPOOFED — visually distinct from every other
      // anomaly so an operator can scan a dense map and pick out
      // "the vessel that's lying about its position" instantly.
      ais_spoofed:      { color: "#ff5cd2", label: "AIS SPOOFED",      glow: true },
      // Teal — declared destination doesn't match actual heading. A
      // step less alarming than dark / spoofed (the vessel is still
      // cooperative, just lying about WHERE it's going) but high-
      // priority enough to glow.
      port_skipping:    { color: "#76e0d2", label: "PORT SKIP",        glow: true },
    },
    actionLabel: {
      task_sar_satellite: "Task SAR satellite",
      dispatch_patrol_aircraft: "Dispatch patrol aircraft",
      alert_coast_guard: "Alert coast guard",
      log_only: "Log only — no dispatch",
    },
    aoiLabel: "AOI: 47.0–48.7°E / 12.4–15.0°S",
    coordFmt: (lon, lat) => `${lon.toFixed(3)}°E ${lat.toFixed(3)}°`,
  },
  wildfire: {
    label: "WILDFIRE · PHASE 02",
    sub: "Thermal anomaly + smoke fusion",
    apiPath: "/wildfire",
    accent: "#f0a830",
    operator: "captain.alvarez@calfire",
    operatorRole: "CAL FIRE duty officer",
    polygons: [
      { type: "wui", lon: -122.625, lat: 38.480, radius: 0.07, label: "SANTA ROSA WUI",
        color: "#ff7846", aspect: 1.0 },
      { type: "wui", lon: -121.080, lat: 38.690, radius: 0.06, label: "PARADISE WUI",
        color: "#ff7846", aspect: 1.0 },
      { type: "fp", lon: -121.890, lat: 38.020, radius: 0.012, label: "MARTINEZ FLARE",
        color: "#888", aspect: 1.0 },
    ],
    typeMeta: {
      fire_event:     { color: "#ff5c5c", label: "FIRE EVENT",      glow: true },
      hotspot:        { color: "#f0a830", label: "HOTSPOT",         glow: false },
      smoke_plume:    { color: "#a8a8b8", label: "SMOKE PLUME",     glow: false },
      false_positive: { color: "#4a5668", label: "SUPPRESSED · FP", glow: false },
    },
    actionLabel: {
      alert_fire_dispatch: "Alert fire dispatch",
      evacuation_advisory: "Issue evacuation advisory",
      request_aerial_recon: "Request aerial recon",
      log_only: "Log only — watchlist",
    },
    aoiLabel: "AOI: Northern California · 5 fixed sensors + LEO/GEO",
    coordFmt: (lon, lat) => `${Math.abs(lon).toFixed(3)}°W ${lat.toFixed(3)}°N`,
  },
};

const PALETTE = {
  bg: "#070c14", surface: "#0e1622", surface2: "#152030",
  border: "rgba(255,255,255,0.07)", borderStrong: "rgba(255,255,255,0.13)",
  text: "#e6edf5", muted: "#7c8aa0", dim: "#4a5668",
  good: "#5fd093", warn: "#f0a830", alert: "#ff5c5c",
};

// =============================================================
//   Map projection
// =============================================================
function makeProjection(width, height, entities, padding = 30) {
  let lonMin = Infinity, lonMax = -Infinity, latMin = Infinity, latMax = -Infinity;
  const visit = (lon, lat) => {
    lonMin = Math.min(lonMin, lon); lonMax = Math.max(lonMax, lon);
    latMin = Math.min(latMin, lat); latMax = Math.max(latMax, lat);
  };
  for (const e of entities) {
    visit(e.lon, e.lat);
    const pts = e.track || (e.obs && e.obs.map(o => [o.lon, o.lat])) || [];
    for (const p of pts) visit(p[0], p[1]);
  }
  if (!isFinite(lonMin)) { lonMin = -1; lonMax = 1; latMin = -1; latMax = 1; }
  const lonPad = Math.max((lonMax - lonMin) * 0.08, 0.01);
  const latPad = Math.max((latMax - latMin) * 0.08, 0.01);
  lonMin -= lonPad; lonMax += lonPad;
  latMin -= latPad; latMax += latPad;
  const w = Math.max(width - padding * 2, 100);
  const h = Math.max(height - padding * 2, 100);
  const proj = (lon, lat) => [
    padding + ((lon - lonMin) / (lonMax - lonMin)) * w,
    padding + ((latMax - lat) / (latMax - latMin)) * h,
  ];
  return { proj, lonMin, lonMax, latMin, latMax, padding };
}

// =============================================================
//   Components
// =============================================================
function Banner() {
  return (
    <div style={{
      background: "repeating-linear-gradient(45deg, #2a1a08 0 12px, #1a1206 12px 24px)",
      borderTop: `1px solid ${PALETTE.warn}55`,
      borderBottom: `1px solid ${PALETTE.warn}55`,
      color: PALETTE.warn, fontFamily: "'IBM Plex Mono', monospace",
      fontSize: 10, letterSpacing: "0.2em",
      padding: "4px 16px", textAlign: "center", userSelect: "none",
    }}>
      DEMO • SYNTHETIC DATA • NOT FOR OPERATIONAL USE
    </div>
  );
}

function Header({ domainKey, setDomainKey, conn, auditHead, auditCount, decisionsCount }) {
  const cfg = DOMAINS[domainKey];
  return (
    <div style={{
      background: PALETTE.surface, borderBottom: `1px solid ${PALETTE.border}`,
      padding: "10px 24px", display: "flex", alignItems: "center", gap: 28,
    }}>
      <div>
        <div style={{
          fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 16,
          fontWeight: 600, color: PALETTE.text, letterSpacing: "0.04em",
        }}>
          SEMPER<span style={{ color: cfg.accent }}>·</span>SAFE
        </div>
        <div style={{
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
          color: PALETTE.muted, letterSpacing: "0.15em", marginTop: 2,
        }}>
          {cfg.label}
        </div>
      </div>

      <div style={{ display: "flex", gap: 0, marginLeft: 8 }}>
        {Object.keys(DOMAINS).map(k => {
          const active = k === domainKey;
          const c = DOMAINS[k];
          return (
            <button key={k} onClick={() => setDomainKey(k)} style={{
              background: active ? c.accent + "18" : "transparent",
              border: `1px solid ${active ? c.accent + "60" : PALETTE.border}`,
              color: active ? c.accent : PALETTE.muted,
              fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
              letterSpacing: "0.18em", padding: "6px 14px",
              cursor: "pointer", fontWeight: 500,
              borderRight: k === "maritime" ? "none" : undefined,
            }}>
              {k.toUpperCase()}
            </button>
          );
        })}
      </div>

      <div style={{ flex: 1 }} />
      <ConnIndicator conn={conn} />
      <KV label="AUDIT HEAD" value={(auditHead || "").slice(0, 16) + "…"} />
      <KV label="ENTRIES"   value={String(auditCount)} />
      <KV label="DECISIONS" value={String(decisionsCount)} />
      <KV label="OPERATOR"  value={cfg.operator} />
    </div>
  );
}

function ConnIndicator({ conn }) {
  const c = conn.status === "live" ? PALETTE.good
          : conn.status === "checking" ? PALETTE.warn
          : PALETTE.dim;
  const label = conn.status === "live" ? "LIVE API"
              : conn.status === "checking" ? "PROBING"
              : "OFFLINE / CACHED";
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 6,
      fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
      color: c, letterSpacing: "0.15em",
      border: `1px solid ${c}40`,
      padding: "4px 10px", background: c + "10",
    }} title={conn.detail || ""}>
      <span style={{
        width: 7, height: 7, borderRadius: "50%", background: c,
        boxShadow: conn.status === "live" ? `0 0 6px ${c}` : "none",
      }} />
      {label}
    </div>
  );
}

function KV({ label, value }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
      <span style={{
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
        color: PALETTE.muted, letterSpacing: "0.18em",
      }}>{label}</span>
      <span style={{
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 12,
        color: PALETTE.text,
      }}>{value}</span>
    </div>
  );
}

function EntityList({ entities, selectedId, onSelect, decisions, cfg }) {
  return (
    <div style={{
      background: PALETTE.surface, borderRight: `1px solid ${PALETTE.border}`,
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      <div style={{
        padding: "12px 14px", borderBottom: `1px solid ${PALETTE.border}`,
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
        color: PALETTE.muted, letterSpacing: "0.2em",
      }}>
        PRIORITY QUEUE · {entities.length}
      </div>
      <div style={{ overflowY: "auto", flex: 1 }}>
        {entities.map(e => {
          const meta = cfg.typeMeta[e.type] || { color: PALETTE.muted, label: e.type };
          const sel = e.id === selectedId;
          const decision = decisions[e.id];
          const suppressed = e.type === "false_positive";
          return (
            <div key={e.id} onClick={() => onSelect(e.id)} style={{
              padding: "10px 14px", borderBottom: `1px solid ${PALETTE.border}`,
              cursor: "pointer",
              background: sel ? PALETTE.surface2 : "transparent",
              borderLeft: `2px solid ${sel ? meta.color : "transparent"}`,
              display: "flex", flexDirection: "column", gap: 4,
              opacity: suppressed ? 0.55 : 1,
              transition: "background 0.1s",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{
                  fontFamily: "'IBM Plex Mono', monospace",
                  fontSize: 10, fontWeight: 600,
                  color: meta.color, letterSpacing: "0.1em",
                  background: meta.color + "15",
                  padding: "1px 5px", border: `1px solid ${meta.color}30`,
                }}>{meta.label}</span>
                <span style={{
                  fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
                  color: PALETTE.text, marginLeft: "auto",
                }}>P {e.priority.toFixed(2)}</span>
              </div>
              <div style={{
                fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 13, color: PALETTE.text,
              }}>
                {e.name || <span style={{ color: PALETTE.muted, fontStyle: "italic" }}>unidentified</span>}
              </div>
              <div style={{
                fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
                color: PALETTE.muted, display: "flex", gap: 10,
              }}>
                <span>{e.id.slice(0, 14)}</span>
                <span>obs:{e.obs_count}</span>
                {decision && (
                  <span style={{ color: decision === "approved" ? PALETTE.good : PALETTE.muted, marginLeft: "auto" }}>
                    ● {decision}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MapView({ entities, selectedId, onSelect, cfg }) {
  const containerRef = useRef(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  useEffect(() => {
    const el = containerRef.current; if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el); return () => ro.disconnect();
  }, []);

  const { proj, lonMin, lonMax, latMin, latMax, padding } = useMemo(
    () => makeProjection(size.w, size.h, entities), [size.w, size.h, entities]
  );

  const lonStep = (lonMax - lonMin) > 6 ? 1 : (lonMax - lonMin) > 1 ? 0.25 : 0.05;
  const latStep = (latMax - latMin) > 6 ? 1 : (latMax - latMin) > 1 ? 0.25 : 0.05;
  const lonLines = [], latLines = [];
  for (let lon = Math.ceil(lonMin / lonStep) * lonStep; lon <= lonMax; lon += lonStep) lonLines.push(lon);
  for (let lat = Math.ceil(latMin / latStep) * latStep; lat <= latMax; lat += latStep) latLines.push(lat);

  return (
    <div ref={containerRef} style={{ position: "relative", flex: 1, background: "#040810", overflow: "hidden" }}>
      <div style={{
        position: "absolute", inset: 0,
        backgroundImage: `linear-gradient(${PALETTE.border} 1px, transparent 1px),
                          linear-gradient(90deg, ${PALETTE.border} 1px, transparent 1px)`,
        backgroundSize: "60px 60px", opacity: 0.4, pointerEvents: "none",
      }} />

      <svg width={size.w} height={size.h} style={{ display: "block" }}>
        <defs>
          {Object.entries(cfg.typeMeta).map(([k, m]) =>
            m.glow && (
              <radialGradient key={k} id={`glow-${k}`} cx="50%" cy="50%" r="50%">
                <stop offset="0%" stopColor={m.color} stopOpacity="0.5" />
                <stop offset="100%" stopColor={m.color} stopOpacity="0" />
              </radialGradient>
            )
          )}
        </defs>

        {/* Graticule */}
        {lonLines.map(lon => {
          const [x] = proj(lon, latMin);
          return <line key={"lon" + lon} x1={x} y1={padding} x2={x} y2={size.h - padding}
            stroke={PALETTE.border} strokeWidth={0.5} />;
        })}
        {latLines.map(lat => {
          const [, y] = proj(lonMin, lat);
          return <line key={"lat" + lat} x1={padding} y1={y} x2={size.w - padding} y2={y}
            stroke={PALETTE.border} strokeWidth={0.5} />;
        })}
        {lonLines.map(lon => {
          const [x] = proj(lon, latMin);
          return <text key={"lonl" + lon} x={x} y={size.h - 12}
            fill={PALETTE.dim} fontSize={9}
            fontFamily="'IBM Plex Mono', monospace" textAnchor="middle">
            {Math.abs(lon).toFixed(2)}°{lon < 0 ? "W" : "E"}
          </text>;
        })}
        {latLines.map(lat => {
          const [, y] = proj(lonMin, lat);
          return <text key={"latl" + lat} x={6} y={y + 3}
            fill={PALETTE.dim} fontSize={9}
            fontFamily="'IBM Plex Mono', monospace">
            {Math.abs(lat).toFixed(2)}°{lat < 0 ? "S" : "N"}
          </text>;
        })}

        {/* Domain polygons (MPA / WUI / FP zones) */}
        {cfg.polygons.map((poly, i) => {
          const pts = [];
          for (let a = 0; a < 36; a++) {
            const ang = (a / 36) * Math.PI * 2;
            pts.push([
              poly.lon + Math.cos(ang) * poly.radius,
              poly.lat + Math.sin(ang) * poly.radius * poly.aspect,
            ]);
          }
          const [lx, ly] = proj(poly.lon, poly.lat + poly.radius * poly.aspect * 0.85);
          return (
            <g key={"poly" + i}>
              <polygon points={pts.map(([lon, lat]) => proj(lon, lat).join(",")).join(" ")}
                fill={poly.color} fillOpacity={poly.type === "fp" ? 0.10 : 0.04}
                stroke={poly.color} strokeOpacity={poly.type === "fp" ? 0.5 : 0.4}
                strokeWidth={1} strokeDasharray={poly.type === "fp" ? "2 2" : "4 3"} />
              <text x={lx} y={ly}
                fill={poly.color} fillOpacity={0.7}
                fontSize={9} fontFamily="'IBM Plex Mono', monospace"
                textAnchor="middle" letterSpacing="0.15em">
                {poly.label}
              </text>
            </g>
          );
        })}

        {/* Tracks (maritime) */}
        {entities.map(e => {
          const meta = cfg.typeMeta[e.type]; if (!meta) return null;
          const sel = e.id === selectedId;
          const track = e.track || (e.obs ? e.obs.map(o => [o.lon, o.lat]) : []);
          if (track.length < 2) return null;
          const path = track.map(([lon, lat], i) => {
            const [x, y] = proj(lon, lat);
            return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
          }).join(" ");
          return (
            <path key={"trk" + e.id} d={path}
              stroke={meta.color}
              strokeWidth={sel ? 2 : 1}
              strokeOpacity={sel ? 0.9 : (e.type === "vessel" ? 0.25 : 0.55)}
              fill="none" strokeLinecap="round" strokeLinejoin="round"
              strokeDasharray={e.type === "ais_gap" ? "3 4" : null} />
          );
        })}

        {/* Multi-source dots for wildfire entities (each obs visible) */}
        {entities.map(e => {
          if (!e.obs || e.obs.length < 2) return null;
          const meta = cfg.typeMeta[e.type]; if (!meta) return null;
          return e.obs.map((o, i) => {
            const [x, y] = proj(o.lon, o.lat);
            const r = o.src === "weather" ? 0 : 2;
            const c = o.src === "viirs" ? "#ffaa55"
                    : o.src === "goes" ? "#ff9966"
                    : o.src === "optical" ? "#cccccc"
                    : meta.color;
            return r > 0 ? (
              <circle key={`obs${e.id}${i}`} cx={x} cy={y} r={r}
                fill={c} fillOpacity={0.8} />
            ) : null;
          });
        })}

        {/* Current positions */}
        {entities.map(e => {
          const meta = cfg.typeMeta[e.type]; if (!meta) return null;
          const sel = e.id === selectedId;
          const [x, y] = proj(e.lon, e.lat);
          const radius = e.type === "vessel" || e.type === "false_positive" ? 3
                       : e.type === "fire_event" ? 6 : 5;
          return (
            <g key={"pt" + e.id} onClick={() => onSelect(e.id)} style={{ cursor: "pointer" }}>
              {meta.glow && (
                <circle cx={x} cy={y} r={22} fill={`url(#glow-${e.type})`} />
              )}
              {sel && (
                <circle cx={x} cy={y} r={radius + 6} fill="none"
                  stroke={meta.color} strokeWidth={1} strokeOpacity={0.6}>
                  <animate attributeName="r" values={`${radius+4};${radius+10};${radius+4}`}
                    dur="2s" repeatCount="indefinite" />
                  <animate attributeName="stroke-opacity" values="0.6;0.1;0.6"
                    dur="2s" repeatCount="indefinite" />
                </circle>
              )}
              {e.type === "false_positive" ? (
                <g>
                  <circle cx={x} cy={y} r={radius} fill="none" stroke={meta.color} strokeWidth={1} />
                  <line x1={x - 4} y1={y - 4} x2={x + 4} y2={y + 4}
                    stroke={meta.color} strokeWidth={1.5} />
                </g>
              ) : (
                <circle cx={x} cy={y} r={radius} fill={meta.color}
                  stroke={PALETTE.bg} strokeWidth={1.5} />
              )}
              {(e.type !== "vessel" && e.type !== "false_positive") && (
                <text x={x + 9} y={y - 6}
                  fill={meta.color} fontSize={9}
                  fontFamily="'IBM Plex Mono', monospace" letterSpacing="0.05em">
                  {e.name || e.id.slice(0, 14)}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      {/* Legend */}
      <div style={{
        position: "absolute", left: 16, top: 16,
        background: PALETTE.surface + "ee", border: `1px solid ${PALETTE.border}`,
        padding: "10px 12px", fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 10, color: PALETTE.text, backdropFilter: "blur(4px)",
        minWidth: 180,
      }}>
        <div style={{ color: PALETTE.muted, marginBottom: 6, letterSpacing: "0.15em" }}>LAYERS</div>
        {Object.entries(cfg.typeMeta).map(([k, m]) => {
          const c = entities.filter(e => e.type === k).length;
          return (
            <div key={k} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: m.color }} />
              <span>{m.label}</span>
              <span style={{ color: PALETTE.muted, marginLeft: "auto" }}>{c}</span>
            </div>
          );
        })}
        <div style={{ borderTop: `1px solid ${PALETTE.border}`, marginTop: 6, paddingTop: 6, color: PALETTE.muted }}>
          {cfg.aoiLabel}
        </div>
      </div>

      <div style={{
        position: "absolute", right: 16, bottom: 16,
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
        color: PALETTE.dim, letterSpacing: "0.15em",
      }}>
        WGS84 · MERCATOR APPROX
      </div>
    </div>
  );
}

// Anomaly types that warrant a "who is this really?" investigation panel.
// dark_vessel: SAR hit with no AIS — need to find candidate hulls nearby.
// ais_gap: vessel went silent — is someone else nearby who might be it?
// loitering_vessel + ais_spoofed: same investigative question, different signal.
const SUSPECT_TYPES = new Set([
  "dark_vessel", "ais_gap", "loitering_vessel", "ais_spoofed", "port_skipping",
]);

// Haversine distance — same formula the backend uses in fusion.haversine_km.
// Pure-JS so the panel doesn't need a network call to rank candidates.
function _distKm(a, b) {
  const R = 6371.0;
  const toRad = (d) => (d * Math.PI) / 180;
  const la1 = toRad(a.lat), la2 = toRad(b.lat);
  const dla = toRad(b.lat - a.lat);
  const dlo = toRad(b.lon - a.lon);
  const h = Math.sin(dla / 2) ** 2 +
            Math.cos(la1) * Math.cos(la2) * Math.sin(dlo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Format an ISO timestamp as a human-friendly "5m ago" relative string.
function _ageStr(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

// Side-panel section that surfaces cooperative AIS vessels in a tight
// radius around an anomaly. The investigative question this answers:
// "we have a dark vessel here / a vessel went dark here / a vessel is
//  spoofing AIS — which of the cooperative hulls nearby could it actually
//  be?" One click drops the operator into the candidate's detail view.
//
// Radius is intentionally tight (5 km). Realistic vessel positions can
// drift by a kilometer or two between consecutive AIS reports, so 5 km
// covers a generous "they could be the same hull observed differently".
// Wider radii flood the list and stop being useful.
const SUSPECT_SEARCH_RADIUS_KM = 5.0;
const SUSPECT_MAX_CANDIDATES = 10;

function NearbyCandidatesSection({ entity, allEntities, onSelect, cfg }) {
  const candidates = useMemo(() => {
    if (entity == null || typeof entity.lon !== "number" ||
        typeof entity.lat !== "number") {
      return [];
    }
    const origin = { lon: entity.lon, lat: entity.lat };
    const rows = [];
    for (const e of allEntities) {
      if (e.id === entity.id) continue;
      // Only consider AIS-cooperative vessels — anomaly-to-anomaly matches
      // aren't investigative leads, they're separate threats.
      if (e.type !== "vessel" && e.type !== "ais_gap") continue;
      if (typeof e.lon !== "number" || typeof e.lat !== "number") continue;
      const d = _distKm(origin, { lon: e.lon, lat: e.lat });
      if (d > SUSPECT_SEARCH_RADIUS_KM) continue;
      rows.push({ ent: e, distance_km: d });
    }
    rows.sort((a, b) => a.distance_km - b.distance_km);
    return rows.slice(0, SUSPECT_MAX_CANDIDATES);
  }, [entity, allEntities]);

  return (
    <div style={{
      padding: "12px 18px",
      borderTop: `1px solid ${PALETTE.border}`,
      background: PALETTE.surface,
    }}>
      <div style={{
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
        color: PALETTE.muted, letterSpacing: "0.2em",
        display: "flex", justifyContent: "space-between", alignItems: "baseline",
      }}>
        <span>NEARBY CANDIDATE HULLS</span>
        <span style={{ color: PALETTE.dim }}>≤ {SUSPECT_SEARCH_RADIUS_KM} KM</span>
      </div>
      {candidates.length === 0 ? (
        <div style={{
          marginTop: 8,
          fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 12,
          color: PALETTE.muted, fontStyle: "italic", lineHeight: 1.4,
        }}>
          No cooperative AIS vessels within {SUSPECT_SEARCH_RADIUS_KM} km.
          This anomaly is isolated — no obvious co-located candidate hull.
        </div>
      ) : (
        <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}>
          {candidates.map(({ ent, distance_km }) => {
            const m = cfg.typeMeta[ent.type] || { color: PALETTE.muted, label: ent.type };
            return (
              <button
                key={ent.id}
                type="button"
                onClick={() => onSelect && onSelect(ent.id)}
                style={{
                  appearance: "none",
                  background: PALETTE.surface2 + "70",
                  border: `1px solid ${PALETTE.border}`,
                  borderRadius: 3,
                  padding: "8px 10px",
                  cursor: onSelect ? "pointer" : "default",
                  textAlign: "left",
                  fontFamily: "'IBM Plex Sans', sans-serif",
                  color: PALETTE.text,
                  display: "grid",
                  gridTemplateColumns: "1fr auto",
                  gap: 4,
                  rowGap: 2,
                }}
                title={`Switch focus to ${ent.name || ent.id}`}
              >
                <span style={{
                  fontSize: 12, fontWeight: 500,
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {ent.name || (
                    <span style={{ color: PALETTE.muted, fontStyle: "italic" }}>
                      unidentified
                    </span>
                  )}
                </span>
                <span style={{
                  fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
                  color: m.color, fontVariantNumeric: "tabular-nums",
                  textAlign: "right",
                }}>
                  {distance_km < 1
                    ? `${Math.round(distance_km * 1000)} m`
                    : `${distance_km.toFixed(1)} km`}
                </span>
                <span style={{
                  fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
                  color: PALETTE.muted, letterSpacing: "0.06em",
                  display: "flex", gap: 6, alignItems: "center",
                }}>
                  <span style={{
                    width: 6, height: 6, borderRadius: "50%",
                    background: m.color, display: "inline-block",
                  }} />
                  <span>{m.label}</span>
                  {ent.mmsi && <span>· MMSI {ent.mmsi}</span>}
                </span>
                <span style={{
                  fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
                  color: PALETTE.muted, textAlign: "right",
                }}>
                  {_ageStr(ent.last_seen)}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// OpticalChipSection — embeds the most recent Sentinel-2 RGB chip
// centered on whatever the operator has selected. Lets you actually
// SEE the spot on the map (down to 10 m S2 resolution; vessels show
// up as bright 1-2 pixel returns over water) rather than just
// reading attrs. Backed by GET /maritime/optical_chip?lat=&lon=
// which finds the most recent cloud-free S2 scene and renders a
// JPEG chip from it.
//
// Returns null silently when the backend says no S2 scene is
// available — the panel just shows the rest of the entity info
// without a placeholder, since the alternative ('no imagery found')
// is noise the operator can't act on.
function OpticalChipSection({ entity, apiBase }) {
  const [state, setState] = useState({ url: null, err: null, loading: true });
  useEffect(() => {
    if (!entity || typeof entity.lon !== "number" || typeof entity.lat !== "number") {
      setState({ url: null, err: null, loading: false });
      return;
    }
    setState((s) => ({ ...s, loading: true }));
    const ctrl = new AbortController();
    const url = `${apiBase}/maritime/optical_chip?lat=${entity.lat}&lon=${entity.lon}`;
    fetch(url, { signal: ctrl.signal })
      .then(async (r) => {
        if (r.status === 404 || r.status === 202) {
          setState({ url: null, err: r.status === 202 ? "scene_pending"
                                                       : "no_recent_scene",
                     loading: false });
          return;
        }
        if (!r.ok) {
          setState({ url: null, err: `HTTP ${r.status}`, loading: false });
          return;
        }
        const blob = await r.blob();
        setState({ url: URL.createObjectURL(blob), err: null, loading: false });
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        setState({ url: null, err: String(err.message || err), loading: false });
      });
    return () => ctrl.abort();
  }, [entity?.id, entity?.lon, entity?.lat, apiBase]);

  // Clean up the object URL when the chip changes / panel unmounts.
  useEffect(() => {
    return () => {
      if (state.url) URL.revokeObjectURL(state.url);
    };
  }, [state.url]);

  if (state.loading) {
    return (
      <div style={{
        padding: "10px 18px", borderTop: `1px solid ${PALETTE.border}`,
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
        color: PALETTE.muted, letterSpacing: "0.15em",
      }}>OPTICAL CHIP · LOADING…</div>
    );
  }
  if (!state.url) {
    // No recent chip available. Render a brief explainer instead of
    // hiding entirely so the operator knows the system tried.
    return (
      <div style={{
        padding: "10px 18px", borderTop: `1px solid ${PALETTE.border}`,
        fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 11,
        color: PALETTE.muted, fontStyle: "italic", lineHeight: 1.4,
      }}>
        No recent cloud-free Sentinel-2 chip available for this point.
        Next S2 pass typically within 2–5 days.
      </div>
    );
  }
  return (
    <div style={{
      padding: "10px 18px", borderTop: `1px solid ${PALETTE.border}`,
    }}>
      <div style={{
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
        color: PALETTE.muted, letterSpacing: "0.2em", marginBottom: 6,
      }}>OPTICAL IMAGERY · SENTINEL-2 · 10 M GSD</div>
      <img
        src={state.url}
        alt="Sentinel-2 chip"
        style={{
          width: "100%",
          display: "block",
          border: `1px solid ${PALETTE.border}`,
          borderRadius: 3,
        }}
      />
    </div>
  );
}

// DispatchSection — sits inside the recommendation block. The
// "compliance story" surface: operator files a formal dispatch, the
// backend appends a hash-chained audit entry, the proof receipt
// renders inline next to the button.
function DispatchSection({ entity, cfg, apiBase }) {
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState(null);
  const [err, setErr] = useState(null);

  if (!entity) return null;
  const defaultAction = entity.recommendation
    ? entity.recommendation.action
    : "log_only";

  const file = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await fetch(`${apiBase}/maritime/dispatches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          operator: cfg.operator,
          entity_id: entity.id,
          action_type: defaultAction,
          notes: "operator-initiated dispatch",
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setReceipt(data);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (receipt) {
    return (
      <div style={{
        marginTop: 12, padding: "10px 12px",
        border: `1px solid ${PALETTE.good}40`,
        background: PALETTE.good + "10",
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
        color: PALETTE.good, letterSpacing: "0.05em",
        lineHeight: 1.6,
      }}>
        <div>● DISPATCH FILED · seq #{receipt.audit_seq}</div>
        <div style={{
          fontSize: 10, color: PALETTE.muted, marginTop: 4,
          wordBreak: "break-all",
        }}>
          hash: {receipt.audit_hash}
        </div>
        <div style={{ fontSize: 10, color: PALETTE.muted }}>
          {receipt.dispatched_at?.slice(11, 19)}Z · {receipt.operator}
        </div>
      </div>
    );
  }

  return (
    <div style={{ marginTop: 12 }}>
      <button
        type="button"
        disabled={busy}
        onClick={file}
        style={{
          appearance: "none",
          border: `1px solid ${PALETTE.alert}`,
          background: PALETTE.alert + "1a",
          color: PALETTE.alert,
          padding: "6px 12px",
          borderRadius: 3,
          fontFamily: "'IBM Plex Mono', monospace",
          fontSize: 10,
          letterSpacing: "0.18em",
          cursor: busy ? "default" : "pointer",
          textTransform: "uppercase",
          fontWeight: 600,
          width: "100%",
        }}
      >
        {busy ? "filing…" : "▸ file dispatch · audit-logged"}
      </button>
      {err && (
        <div style={{
          marginTop: 6,
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
          color: PALETTE.alert,
        }}>
          error: {err}
        </div>
      )}
    </div>
  );
}

function LineagePanel({ entity, allEntities, onSelect, decisions, onApprove, onReject, cfg }) {
  if (!entity) {
    return (
      <div style={{
        flexBasis: 380, flexShrink: 0, background: PALETTE.surface,
        borderLeft: `1px solid ${PALETTE.border}`, padding: 24,
        color: PALETTE.muted, fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 11, letterSpacing: "0.1em",
      }}>
        SELECT AN ENTITY TO INSPECT LINEAGE
      </div>
    );
  }
  const meta = cfg.typeMeta[entity.type] || { color: PALETTE.muted, label: entity.type };
  const decision = decisions[entity.id];

  const chain = useMemo(() => {
    const events = [{
      t: entity.first_seen.slice(11, 19),
      type: "create",
      text: `Entity created (${entity.type})`,
      hash: hashStr(entity.id + "create"),
    }];
    const obsList = entity.obs || (entity.track ? entity.track.map(t => ({ lon: t[0], lat: t[1], src: "ais" })) : []);
    obsList.slice(0, 6).forEach((o, i) => {
      events.push({
        t: entity.first_seen.slice(11, 19),
        type: "obs",
        text: `${(o.src || "obs").toUpperCase()} obs · ${o.lon.toFixed(3)}, ${o.lat.toFixed(3)}`,
        hash: hashStr(entity.id + "obs" + i),
      });
    });
    if (entity.type === "fire_event") {
      events.push({ t: entity.last_seen.slice(11, 19), type: "reclassify",
        text: "Hotspot → fire_event (persistence + smoke corroboration)",
        hash: hashStr(entity.id + "reclass") });
    }
    if (entity.type === "ais_gap") {
      events.push({ t: entity.last_seen.slice(11, 19), type: "reclassify",
        text: "AIS dropout exceeds 15-min threshold → ais_gap",
        hash: hashStr(entity.id + "reclass") });
    }
    if (entity.type === "false_positive") {
      events.push({ t: entity.last_seen.slice(11, 19), type: "suppress",
        text: `Suppressed against known FP source: ${entity.name || "industrial"}`,
        hash: hashStr(entity.id + "fp") });
    }
    if (entity.recommendation) {
      events.push({ t: entity.last_seen.slice(11, 19), type: "recommend",
        text: `Recommendation: ${cfg.actionLabel[entity.recommendation.action] || entity.recommendation.action}`,
        hash: hashStr(entity.recommendation.rec_id) });
    }
    return events;
  }, [entity, cfg]);

  const weather = entity.attrs && entity.attrs.weather;

  return (
    <div style={{
      flexBasis: 380, flexShrink: 0, background: PALETTE.surface,
      borderLeft: `1px solid ${PALETTE.border}`,
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      <div style={{ padding: "16px 18px", borderBottom: `1px solid ${PALETTE.border}` }}>
        <div style={{
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
          color: PALETTE.muted, letterSpacing: "0.2em",
        }}>ENTITY DETAIL</div>
        <div style={{
          fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 17,
          color: PALETTE.text, marginTop: 4, fontWeight: 500,
        }}>
          {entity.name || <span style={{ color: PALETTE.muted, fontStyle: "italic" }}>unidentified</span>}
        </div>
        <div style={{
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
          color: PALETTE.muted, marginTop: 3,
        }}>{entity.id}</div>
        <div style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
          <Tag color={meta.color}>{meta.label}</Tag>
          {entity.mmsi && <Tag>MMSI {entity.mmsi}</Tag>}
          {entity.vtype && <Tag>{entity.vtype.toUpperCase()}</Tag>}
          {entity.attrs && entity.attrs.frp_mw &&
            <Tag color={cfg.accent}>FRP {entity.attrs.frp_mw} MW</Tag>}
        </div>
      </div>

      <div style={{
        padding: "14px 18px", borderBottom: `1px solid ${PALETTE.border}`,
        display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px 16px",
      }}>
        <Stat label="PRIORITY"   value={entity.priority.toFixed(2)} />
        <Stat label="CONFIDENCE" value={entity.confidence.toFixed(2)} />
        <Stat label="FIRST SEEN" value={entity.first_seen.slice(11, 19) + "Z"} />
        <Stat label="LAST SEEN"  value={entity.last_seen.slice(11, 19) + "Z"} />
        <Stat label="POSITION"   value={cfg.coordFmt(entity.lon, entity.lat)} />
        <Stat label="OBS COUNT"  value={String(entity.obs_count)} />
      </div>

      {weather && (
        <div style={{
          padding: "10px 18px", borderBottom: `1px solid ${PALETTE.border}`,
          background: weather.advisory ? "#3a1a08" : PALETTE.surface2 + "70",
        }}>
          <div style={{
            fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
            color: weather.advisory ? PALETTE.warn : PALETTE.muted,
            letterSpacing: "0.2em", marginBottom: 6,
          }}>
            WEATHER CONTEXT {weather.advisory && `· ${weather.advisory}`}
          </div>
          <div style={{
            fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
            color: PALETTE.text, display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6,
          }}>
            <span>RH {weather.rh_pct}%</span>
            <span>WIND {weather.wind_mph}mph</span>
            <span>FUEL {weather.fuel_moisture}%</span>
          </div>
        </div>
      )}

      {entity.notes && (
        <div style={{
          padding: "10px 18px", borderBottom: `1px solid ${PALETTE.border}`,
          fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 12,
          color: PALETTE.text, opacity: 0.85, fontStyle: "italic",
          background: PALETTE.surface2 + "70",
        }}>
          {entity.notes}
        </div>
      )}

      {/* Optical chip — actual Sentinel-2 imagery of the selected
          vessel's location. Renders inline for ANY selected entity,
          not just SAR detections. */}
      <OpticalChipSection entity={entity} apiBase={API_BASE} />

      <div style={{ padding: "14px 18px", flex: 1, overflowY: "auto" }}>
        <div style={{
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
          color: PALETTE.muted, letterSpacing: "0.2em", marginBottom: 10,
        }}>LINEAGE CHAIN · {chain.length} EVENTS</div>
        <div style={{ position: "relative" }}>
          <div style={{
            position: "absolute", left: 7, top: 6, bottom: 6,
            width: 1, background: PALETTE.border,
          }} />
          {chain.map((c, i) => (
            <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 10, position: "relative" }}>
              <div style={{
                width: 15, height: 15, borderRadius: "50%", background: PALETTE.bg,
                border: `1.5px solid ${
                  c.type === "create" ? cfg.accent :
                  c.type === "reclassify" ? PALETTE.warn :
                  c.type === "suppress" ? PALETTE.dim :
                  c.type === "recommend" ? PALETTE.alert : PALETTE.muted
                }`, flexShrink: 0, marginTop: 1,
              }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 12,
                  color: PALETTE.text, lineHeight: 1.4,
                }}>{c.text}</div>
                <div style={{
                  fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
                  color: PALETTE.muted, marginTop: 2,
                  display: "flex", gap: 10,
                }}>
                  <span>{c.t}Z</span>
                  <span>#{c.hash.slice(0, 10)}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {SUSPECT_TYPES.has(entity.type) && (
        <NearbyCandidatesSection
          entity={entity}
          allEntities={allEntities || []}
          onSelect={onSelect}
          cfg={cfg}
        />
      )}

      {entity.recommendation && (
        <div style={{
          padding: "16px 18px", borderTop: `1px solid ${PALETTE.borderStrong}`,
          background: PALETTE.surface2,
        }}>
          <div style={{
            fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
            color: PALETTE.muted, letterSpacing: "0.2em",
          }}>RECOMMENDED ACTION</div>
          <div style={{
            fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 14,
            color: PALETTE.text, fontWeight: 500, marginTop: 6,
          }}>
            {cfg.actionLabel[entity.recommendation.action] || entity.recommendation.action}
          </div>
          <div style={{
            fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 12,
            color: PALETTE.muted, marginTop: 6, lineHeight: 1.5,
          }}>{entity.recommendation.rationale}</div>
          {decision ? (
            <div style={{
              marginTop: 14, padding: "10px 12px",
              border: `1px solid ${decision === "approved" ? PALETTE.good : PALETTE.muted}40`,
              background: (decision === "approved" ? PALETTE.good : PALETTE.muted) + "10",
              fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
              color: decision === "approved" ? PALETTE.good : PALETTE.muted,
              letterSpacing: "0.05em",
            }}>
              {decision.toUpperCase()} · {cfg.operator} · logged
            </div>
          ) : (
            <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
              <button onClick={() => onApprove(entity.id)} style={btn(PALETTE.good)}>APPROVE</button>
              <button onClick={() => onReject(entity.id)} style={btn(PALETTE.muted)}>REJECT</button>
            </div>
          )}
          {/* Always-available formal dispatch action — independent of
              the pending-recommendation accept/reject flow. Hash-chained
              into the audit log for compliance. */}
          <DispatchSection entity={entity} cfg={cfg} apiBase={API_BASE} />
        </div>
      )}

      {/* For entities with no engine recommendation (e.g. routine
          cooperative vessels selected for inspection), still expose
          the dispatch action — sometimes operators just want to file
          a "checked this vessel" record. */}
      {!entity.recommendation && SUSPECT_TYPES.has(entity.type) && (
        <div style={{
          padding: "16px 18px", borderTop: `1px solid ${PALETTE.borderStrong}`,
          background: PALETTE.surface2,
        }}>
          <div style={{
            fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
            color: PALETTE.muted, letterSpacing: "0.2em",
          }}>OPERATOR DISPATCH</div>
          <DispatchSection entity={entity} cfg={cfg} apiBase={API_BASE} />
        </div>
      )}
    </div>
  );
}

function Tag({ children, color }) {
  return (
    <span style={{
      fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
      letterSpacing: "0.15em",
      color: color || PALETTE.muted,
      border: `1px solid ${color ? color + "40" : PALETTE.border}`,
      background: color ? color + "12" : "transparent",
      padding: "2px 7px",
    }}>{children}</span>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <div style={{
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 9,
        color: PALETTE.muted, letterSpacing: "0.18em",
      }}>{label}</div>
      <div style={{
        fontFamily: "'IBM Plex Mono', monospace", fontSize: 12,
        color: PALETTE.text, marginTop: 2,
      }}>{value}</div>
    </div>
  );
}

function btn(color) {
  return {
    flex: 1, background: color + "18", border: `1px solid ${color}50`, color,
    fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
    letterSpacing: "0.18em", padding: "9px 12px",
    cursor: "pointer", fontWeight: 600,
  };
}

// AuditFeed — bottom-of-screen scrolling event log, hash-chained.
// Tab strip lets the operator filter:
//   ALL          — every event (default; matches the original behavior)
//   DISPATCHES   — only operator decisions (event_type='decision'). This
//                  is the "compliance story" view: every approve/reject
//                  on a recommendation, the operator who made the call,
//                  and the audit hash that proves it. Each dispatch row
//                  is rendered with extra prominence (action pill,
//                  operator highlight) so it reads more like a logbook.
//   RECLASS      — only reclassifications (dark→matched, vessel→ais_gap,
//                  vessel→loitering_vessel, vessel→ais_spoofed).
//                  Operationally interesting: shows when the engine
//                  changed its mind.
const AUDIT_FILTERS = {
  ALL: { label: "ALL", match: () => true },
  DISPATCHES: { label: "DISPATCHES", match: (e) => e.event === "decision" },
  RECLASS: {
    label: "RECLASS",
    match: (e) => e.event === "entity_reclassified",
  },
};

function _decisionPill(text, color) {
  return (
    <span style={{
      display: "inline-block",
      padding: "1px 7px",
      borderRadius: 2,
      background: `${color}26`,
      border: `1px solid ${color}`,
      color,
      fontFamily: "'IBM Plex Mono', monospace",
      fontSize: 9,
      letterSpacing: "0.12em",
      fontWeight: 600,
      textTransform: "uppercase",
    }}>{text}</span>
  );
}

function AuditFeed({ entries }) {
  const [filter, setFilter] = useState("ALL");

  const eventColor = (et) => {
    if (et.includes("entity_created")) return PALETTE.good;
    if (et.includes("recommendation")) return PALETTE.alert;
    if (et.includes("reclassified") || et.includes("priority")) return PALETTE.warn;
    if (et.includes("decision")) return PALETTE.good;
    if (et.includes("suppressed")) return PALETTE.muted;
    if (et.includes("domain_loaded")) return PALETTE.text;
    return PALETTE.muted;
  };

  const visible = entries.filter(AUDIT_FILTERS[filter].match);

  return (
    <div style={{
      borderTop: `1px solid ${PALETTE.border}`, background: PALETTE.bg,
      height: 200, display: "flex", flexDirection: "column",
    }}>
      <div style={{
        padding: "8px 16px", borderBottom: `1px solid ${PALETTE.border}`,
        display: "flex", alignItems: "center", gap: 14,
      }}>
        <span style={{
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
          color: PALETTE.muted, letterSpacing: "0.2em",
        }}>AUDIT STREAM · HASH-CHAINED</span>
        <span style={{
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
          color: PALETTE.good,
        }}>● VERIFIED</span>
        <div style={{ display: "flex", gap: 4, marginLeft: 8 }}>
          {Object.entries(AUDIT_FILTERS).map(([k, def]) => {
            const active = k === filter;
            const count = entries.filter(def.match).length;
            return (
              <button
                key={k}
                type="button"
                onClick={() => setFilter(k)}
                style={{
                  appearance: "none",
                  border: `1px solid ${active ? PALETTE.accent || "#5dd6c4" : PALETTE.border}`,
                  background: active ? `${PALETTE.accent || "#5dd6c4"}26` : "transparent",
                  color: active ? "#cdf2dd" : PALETTE.muted,
                  borderRadius: 3,
                  padding: "2px 8px",
                  fontFamily: "'IBM Plex Mono', monospace",
                  fontSize: 9,
                  letterSpacing: "0.12em",
                  cursor: "pointer",
                  textTransform: "uppercase",
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <span>{def.label}</span>
                <span style={{
                  fontVariantNumeric: "tabular-nums",
                  color: active ? "#cdf2dd" : PALETTE.dim,
                }}>{count}</span>
              </button>
            );
          })}
        </div>
        <span style={{ flex: 1 }} />
        <span style={{
          fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
          color: PALETTE.muted,
        }}>↑ to oversight board · daily</span>
      </div>
      <div style={{ overflowY: "auto", flex: 1, padding: "4px 0" }}>
        {visible.length === 0 && (
          <div style={{
            padding: "20px 16px",
            fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
            color: PALETTE.muted, fontStyle: "italic",
          }}>
            No matching events. {filter === "DISPATCHES"
              && "Operator decisions will appear here as recommendations are approved/rejected."}
          </div>
        )}
        {visible.slice().reverse().map((e, i) => {
          // Special-case DISPATCH rendering — the "compliance story" line:
          // operator highlight, action pill, hash proof.
          if (e.event === "decision" && filter !== "ALL") {
            const payload = e.payload || {};
            const decided = String(payload.decision || "").toLowerCase();
            const pillColor =
              decided === "approved" ? PALETTE.good
              : decided === "rejected" ? PALETTE.alert
              : PALETTE.warn;
            return (
              <div key={i} style={{
                padding: "8px 16px", display: "grid",
                gridTemplateColumns: "60px 90px 1fr 110px",
                gap: 14, alignItems: "center",
                fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
                color: PALETTE.text,
                borderBottom: `1px solid ${PALETTE.border}`,
                background: PALETTE.surface2 + "30",
              }}>
                <span style={{ color: PALETTE.muted }}>#{e.seq.toString().padStart(4, "0")}</span>
                <span style={{ color: PALETTE.muted }}>{e.t}Z</span>
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  {_decisionPill(decided || "decision", pillColor)}
                  <span style={{ color: PALETTE.text, fontWeight: 500 }}>
                    {payload.entity_id || "—"}
                  </span>
                  <span style={{ color: PALETTE.dim }}>by</span>
                  <span style={{ color: PALETTE.accent || "#5dd6c4" }}>{e.actor}</span>
                  {payload.reason && (
                    <span style={{
                      color: PALETTE.muted, fontStyle: "italic",
                      overflow: "hidden", textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}>
                      · {payload.reason}
                    </span>
                  )}
                </span>
                <span style={{ color: PALETTE.dim, textAlign: "right" }}>
                  {e.hash || `#${hashStr("seq" + e.seq).slice(0, 10)}`}
                </span>
              </div>
            );
          }
          // Default row layout — matches the original AuditFeed shape.
          return (
            <div key={i} style={{
              padding: "4px 16px", display: "grid",
              gridTemplateColumns: "60px 80px 130px 1fr 110px",
              gap: 14, alignItems: "center",
              fontFamily: "'IBM Plex Mono', monospace", fontSize: 11,
              color: PALETTE.text, borderBottom: `1px solid ${PALETTE.border}`,
            }}>
              <span style={{ color: PALETTE.muted }}>#{e.seq.toString().padStart(4, "0")}</span>
              <span style={{ color: PALETTE.muted }}>{e.t}Z</span>
              <span style={{ color: PALETTE.dim }}>{e.actor}</span>
              <span>
                <span style={{ color: eventColor(e.event), fontWeight: 500 }}>{e.event}</span>
                <span style={{ color: PALETTE.muted, marginLeft: 10 }}>{e.note}</span>
              </span>
              <span style={{ color: PALETTE.dim, textAlign: "right" }}>
                {e.hash || `#${hashStr("seq" + e.seq).slice(0, 10)}`}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// =============================================================
//   Utility
// =============================================================
function hashStr(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = (h * 16777619) >>> 0;
  }
  return h.toString(16).padStart(10, "0").repeat(2);
}

// =============================================================
//   Live API client
// =============================================================
async function tryFetch(url, opts = {}) {
  const ctrl = new AbortController();
  // 1500ms was the original timeout when /health returned in <100ms.
  // Bumped to 20s after a recurring user complaint of "everything goes
  // away and goes back to Madagascar": on slower backend states (e.g.
  // when the audit-log scan in /health is taking 4-11 s) the 1500ms
  // budget fires, this throws, Workbench flips to conn.status='offline'
  // and falls back to the embedded MARITIME scenario — which has
  // Madagascar seed entities. The Texas data was always there in the
  // backend; the frontend just gave up before it loaded. A 20s budget
  // covers the worst observed /health latency comfortably without
  // making the "actually offline" failure mode meaningfully slower.
  const tid = setTimeout(() => ctrl.abort(), 20000);
  try {
    const r = await fetch(url, { ...opts, signal: ctrl.signal });
    clearTimeout(tid);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (err) {
    clearTimeout(tid);
    throw err;
  }
}

// =============================================================
//   Root
// =============================================================
export default function Workbench() {
  const [domainKey, setDomainKey] = useState("maritime");
  const [conn, setConn] = useState({ status: "checking", detail: "" });
  const [liveData, setLiveData] = useState({ maritime: null, wildfire: null, audit_head: null, audit_count: null });
  const [selection, setSelection] = useState({ maritime: null, wildfire: null });
  const [decisions, setDecisions] = useState({});
  const [auditExtras, setAuditExtras] = useState([]);

  // Probe API once on mount
  useEffect(() => {
    let canceled = false;
    (async () => {
      try {
        const health = await tryFetch(`${API_BASE}/health`);
        if (canceled) return;
        const [m, f] = await Promise.all([
          tryFetch(`${API_BASE}/maritime/entities`),
          tryFetch(`${API_BASE}/wildfire/entities`),
        ]);
        if (canceled) return;
        setLiveData({
          maritime: m.entities,
          wildfire: f.entities,
          audit_head: health.audit_head,
          audit_count: health.audit_entries,
        });
        setConn({ status: "live", detail: `Backend at ${API_BASE}` });
      } catch (err) {
        if (!canceled) setConn({ status: "offline",
          detail: `No backend at ${API_BASE} — using cached scenario` });
      }
    })();
    return () => { canceled = true; };
  }, []);

  const cfg = DOMAINS[domainKey];

  // Pick data source: live if available, else embedded
  const entities = useMemo(() => {
    if (liveData[domainKey]) {
      // Live API returns full Entity objects with snake_case fields
      return liveData[domainKey].map(e => ({
        id: e.entity_id, type: e.type,
        lon: e.geom.lon, lat: e.geom.lat,
        priority: e.priority_score, confidence: e.confidence,
        first_seen: e.first_seen, last_seen: e.last_seen,
        name: e.attrs?.name || null,
        mmsi: e.attrs?.mmsi, vtype: e.attrs?.type,
        notes: e.notes || "", obs_count: e.observation_ids?.length || 0,
        track: [], obs: [], attrs: e.attrs,
        recommendation: null,  // would fetch from /recommendations in production
      }));
    }
    return (domainKey === "maritime" ? MARITIME : WILDFIRE).entities;
  }, [domainKey, liveData]);

  const sortedEntities = useMemo(() =>
    [...entities].sort((a, b) =>
      b.priority - a.priority ||
      new Date(b.last_seen) - new Date(a.last_seen)
    ), [entities]);

  // Initialize selection when domain changes or data arrives
  useEffect(() => {
    if (sortedEntities.length && !selection[domainKey]) {
      setSelection(s => ({ ...s, [domainKey]: sortedEntities[0].id }));
    }
  }, [domainKey, sortedEntities, selection]);

  const selectedId = selection[domainKey];
  const selected = sortedEntities.find(e => e.id === selectedId);

  const handleDecision = useCallback(async (id, decision) => {
    if (decisions[id]) return;
    setDecisions(d => ({ ...d, [id]: decision }));
    const seq = (liveData.audit_count || 1800) + auditExtras.length + 1;
    const t = new Date().toISOString().slice(11, 19);
    setAuditExtras(a => [...a, {
      seq, t, actor: cfg.operator,
      // Match the backend's audit event name ('decision') so the
      // DISPATCHES filter in AuditFeed picks up this row too.
      event: "decision",
      note: `${decision} · ${id.slice(0, 16)}`,
      payload: {
        rec_id: null,
        entity_id: id,
        decision,
        reason: decision === "approved" ? "operator approved" : "operator rejected",
      },
    }]);
    // Try to POST to live API; ignore failures (offline mode)
    if (conn.status === "live") {
      try {
        await fetch(`${API_BASE}${cfg.apiPath}/actions/${id}/${decision === "approved" ? "approve" : "reject"}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operator: cfg.operator,
                                 reason: decision === "approved" ? "operator approved" : "operator rejected" }),
        });
      } catch (_) { /* offline already handled */ }
    }
  }, [decisions, auditExtras, conn.status, cfg, liveData.audit_count]);

  const allAudit = useMemo(() =>
    [...INITIAL_AUDIT[domainKey], ...auditExtras],
    [domainKey, auditExtras]
  );

  const auditHead = liveData.audit_head ||
    (domainKey === "maritime" ? MARITIME.audit_head : WILDFIRE.audit_head);
  const auditCount = (liveData.audit_count ||
    (domainKey === "maritime" ? MARITIME.audit_count : WILDFIRE.audit_count)) +
    auditExtras.length;

  return (
    <>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet" />
      <div style={{
        height: "100vh", display: "flex", flexDirection: "column",
        background: PALETTE.bg, color: PALETTE.text,
        fontFamily: "'IBM Plex Sans', sans-serif", overflow: "hidden",
      }}>
        <Banner />
        <Header
          domainKey={domainKey} setDomainKey={setDomainKey}
          conn={conn}
          auditHead={auditHead}
          auditCount={auditCount}
          decisionsCount={Object.keys(decisions).length}
        />
        <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
          <div style={{ width: 280, flexShrink: 0 }}>
            <EntityList
              entities={sortedEntities}
              selectedId={selectedId}
              onSelect={(id) => setSelection(s => ({ ...s, [domainKey]: id }))}
              decisions={decisions}
              cfg={cfg}
            />
          </div>
          <MapLibreView
            entities={sortedEntities}
            selectedId={selectedId}
            onSelect={(id) => setSelection(s => ({ ...s, [domainKey]: id }))}
            cfg={cfg}
          />
          <LineagePanel
            entity={selected}
            allEntities={sortedEntities}
            onSelect={(id) => setSelection(s => ({ ...s, [domainKey]: id }))}
            decisions={decisions}
            onApprove={(id) => handleDecision(id, "approved")}
            onReject={(id) => handleDecision(id, "rejected")}
            cfg={cfg}
          />
        </div>
        <AuditFeed entries={allAudit} />
      </div>
    </>
  );
}
