/* eslint-disable react/prop-types */
//
// MapLibre-backed map view. Drop-in for the SVG MapView in Workbench.jsx —
// same prop interface: { entities, selectedId, onSelect, cfg }.
//
// Why this exists: with real-time AIS landing 100s of vessels in the Texas
// AOI, the SVG plot doesn't pan/zoom and entities pile on top of each other.
// MapLibre gives us a real basemap (OpenFreeMap dark, free, no API key),
// pannable/zoomable, with proper geographic projection.
//
// Style: per the Palantir-style discussion + blueprint Layer 5, this lives
// alongside the existing dark workbench chrome rather than replacing it.
// The map IS the navigation surface; the lineage panel + audit feed remain
// the inspectability surface.

import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

// Two basemap options. User can flip between them at runtime.
//   dark — OpenFreeMap vector (free, no key). Clean operator UI for triage.
//   satellite — Esri World Imagery raster (free, attribution required).
//     Same provider Maxar uses for its Vivid product. Imagery is months
//     to years old (NOT real-time satellite — see operator notes).
const BASEMAPS = {
  dark: {
    label: "Dark",
    style: "https://tiles.openfreemap.org/styles/dark",
  },
  satellite: {
    label: "Satellite",
    style: {
      version: 8,
      sources: {
        "esri-world-imagery": {
          type: "raster",
          tiles: [
            "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
          ],
          tileSize: 256,
          maxzoom: 19,
          attribution:
            "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, GeoEye, Earthstar Geographics, USGS, AeroGRID, IGN, IGP",
        },
      },
      layers: [
        {
          id: "esri-world-imagery-layer",
          type: "raster",
          source: "esri-world-imagery",
        },
      ],
      glyphs:
        "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    },
  },
  // EOX Sentinel-2 Cloudless mosaic — a global true-color composite
  // assembled from the cleanest Sentinel-2 captures of the past year,
  // served as 10 m WMTS tiles. Free, public, no key needed. Much
  // closer to "I can see vessels and structures" than the Esri layer
  // (which is a generic basemap blend), and aligns visually with the
  // S2 chip popups we already serve for SAR detections.
  s2cloudless: {
    label: "Sentinel-2",
    style: {
      version: 8,
      sources: {
        "eox-s2cloudless": {
          type: "raster",
          tiles: [
            "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg",
          ],
          tileSize: 256,
          maxzoom: 17,
          attribution:
            "Sentinel-2 cloudless &mdash; <a href='https://s2maps.eu'>s2maps.eu</a> " +
            "by <a href='https://eox.at'>EOX IT Services GmbH</a> " +
            "(Contains modified Copernicus Sentinel data 2023 &amp; 2024)",
        },
      },
      layers: [
        {
          id: "eox-s2cloudless-layer",
          type: "raster",
          source: "eox-s2cloudless",
        },
      ],
      glyphs:
        "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    },
  },
};
const DEFAULT_BASEMAP = "satellite";
const BASEMAP_STORAGE_KEY = "ss-basemap";

// Default view: Texas shoreline AOI center + zoom that shows Galveston Bay
// to Brownsville on a typical screen.
const DEFAULT_CENTER = [-95.5, 28.5];
const DEFAULT_ZOOM = 5.5;

const FIT_PADDING = 60;

// Bounding box clamp for the first-data auto-fit. Backed-end entities
// occasionally drift outside the operator's actual AOI — for example the
// initial Madagascar demo seed left a handful of dark-vessel rows around
// (48.2, -13.7) which would force the camera to span both continents and
// shrink the Texas vessels to invisible specks. We only let the auto-fit
// consider points inside this clamp; if a real entity is outside, that's
// a data-cleanup problem, not a map UX problem.
const FIT_CLAMP_MIN_LON = -98.5;
const FIT_CLAMP_MAX_LON = -93.0;
const FIT_CLAMP_MIN_LAT = 25.0;
const FIT_CLAMP_MAX_LAT = 31.0;
function _insideFitClamp(lon, lat) {
  return (
    typeof lon === "number" && typeof lat === "number"
    && lon >= FIT_CLAMP_MIN_LON && lon <= FIT_CLAMP_MAX_LON
    && lat >= FIT_CLAMP_MIN_LAT && lat <= FIT_CLAMP_MAX_LAT
  );
}

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

const TRACK_SOURCE_ID = "ss-selected-track";
const TRACK_LAYER_ID = "ss-selected-track-line";
const TRACK_HEAD_LAYER_ID = "ss-selected-track-head";

// Pinned-for-comparison entity track — mirrors the SELECTED track but
// in amber so the two tracks are visually distinguishable when an
// operator is comparing trajectories.
const PINNED_TRACK_SOURCE_ID = "ss-pinned-track";
const PINNED_TRACK_LAYER_ID = "ss-pinned-track-line";
const PINNED_HEAD_LAYER_ID = "ss-pinned-track-head";
const PINNED_TRACK_COLOR = "#ffd897";

// Candidate-hull halos — when an anomaly entity is selected, the 5-km
// neighborhood of AIS-cooperative vessels gets ringed on the map so
// the operator sees the side-panel "candidate hulls" list spatially.
const CANDIDATES_SOURCE_ID = "ss-candidate-hulls";
const CANDIDATES_LAYER_ID = "ss-candidate-hulls-ring";
const CANDIDATES_PULSE_LAYER_ID = "ss-candidate-hulls-pulse";

// Convoy visualization — a thin cyan line connecting members of the
// same backend-tagged convoy. Updated whenever the entities prop
// changes (which happens on every AIS observation tick).
const CONVOY_SOURCE_ID = "ss-convoys";
const CONVOY_LINE_LAYER_ID = "ss-convoys-line";
const CONVOY_HULL_LAYER_ID = "ss-convoys-hull";

// Custom AOI drawing — the operator clicks polygon corners to define
// an arbitrary area of interest. Vessels inside get a highlight + count.
const AOI_DRAW_SOURCE_ID = "ss-aoi-draw";
const AOI_DRAW_FILL_LAYER_ID = "ss-aoi-draw-fill";
const AOI_DRAW_LINE_LAYER_ID = "ss-aoi-draw-line";
const AOI_DRAW_VERTEX_LAYER_ID = "ss-aoi-draw-vertex";

// Anomaly vessel trails — last-hour track polylines drawn behind every
// anomaly entity so the map tells the "where have these been" story
// at a glance. Distinct from the per-selection TRACK_SOURCE — these
// render for ALL anomalies simultaneously, in their type-meta color.
const ANOMALY_TRAILS_SOURCE_ID = "ss-anomaly-trails";
const ANOMALY_TRAILS_LAYER_ID = "ss-anomaly-trails-line";

// Activity heatmap — renders current vessel positions as a kernel-
// density heatmap. Helps distinguish 'dark vessel in busy shipping
// lane' (probably benign) from 'dark vessel in empty water'
// (suspicious). Toggleable via a chip in the basemap toggle bar.
const HEATMAP_SOURCE_ID = "ss-activity-heatmap";
const HEATMAP_LAYER_ID = "ss-activity-heatmap-layer";
const HEATMAP_STORAGE_KEY = "ss-activity-heatmap";

// Operator-workload heatmap — overlay where dispatch_filed audit
// entries landed in the last 24 h. Distinct palette (purple→pink)
// so it's visually separable from the cooperative-traffic heatmap.
const WORKLOAD_SOURCE_ID = "ss-workload-heatmap";
const WORKLOAD_LAYER_ID = "ss-workload-heatmap-layer";
const WORKLOAD_STORAGE_KEY = "ss-workload-heatmap";
const WORKLOAD_REFRESH_MS = 90 * 1000;   // dispatches are rare; 90s is plenty

// Onshore assets — USCG stations + Navy facilities. Static data;
// fetched once on toggle-on.
const ASSETS_SOURCE_ID = "ss-onshore-assets";
const ASSETS_LAYER_ID = "ss-onshore-assets-layer";
const ASSETS_LABEL_LAYER_ID = "ss-onshore-assets-label";
const ASSETS_STORAGE_KEY = "ss-onshore-assets";

// Active-dispatch state — once an operator files a dispatch, the
// responsible entity stays ringed on the map for ACTIVE_DISPATCH_TTL_MS.
// Lets the operator visually track 'what I'm currently watching' as
// the underlying anomaly state changes (e.g. a dark vessel that's now
// been AIS-matched still reads as 'under dispatch').
const ACTIVE_DISPATCH_TTL_MS = 4 * 60 * 60 * 1000;   // 4 hours
const ACTIVE_DISPATCH_STORAGE_KEY = "ss-active-dispatches";
const ACTIVE_DISPATCH_SOURCE_ID = "ss-active-dispatch";
const ACTIVE_DISPATCH_RING_LAYER_ID = "ss-active-dispatch-ring";
const ACTIVE_DISPATCH_LABEL_LAYER_ID = "ss-active-dispatch-label";

// Cap how many trails we fetch — saves the backend from N parallel
// /entities/{id}/track hits when there are 50+ anomalies on the map.
// 25 covers all dark/spoofed/port-skipping vessels in normal operation
// and the most-recent loitering ones; ais_gap is excluded entirely
// because there are 100s of those during normal traffic and the
// trails would clutter the map.
const ANOMALY_TRAIL_MAX = 25;
const ANOMALY_TRAIL_TYPES = new Set([
  "dark_vessel", "ais_spoofed", "port_skipping", "loitering_vessel",
]);

// Ray-casting point-in-polygon. Coordinates in [lon, lat] order.
function _pointInPolygon(lon, lat, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const xi = polygon[i][0], yi = polygon[i][1];
    const xj = polygon[j][0], yj = polygon[j][1];
    const intersect = (yi > lat) !== (yj > lat)
      && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

const SUSPECT_TYPES_MAP = new Set([
  "dark_vessel", "ais_gap", "loitering_vessel", "ais_spoofed", "port_skipping",
]);
const CANDIDATE_RADIUS_KM = 5.0;
const CANDIDATE_MAX = 10;

// Pure-JS haversine — same formula used by the backend fusion engine.
// Inlined here so the candidate-halo layer can recompute without a
// network round trip.
function _haversineKm(a, b) {
  const R = 6371.0;
  const toRad = (d) => (d * Math.PI) / 180;
  const la1 = toRad(a.lat), la2 = toRad(b.lat);
  const dla = toRad(b.lat - a.lat);
  const dlo = toRad(b.lon - a.lon);
  const h = Math.sin(dla / 2) ** 2 +
            Math.cos(la1) * Math.cos(la2) * Math.sin(dlo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Sentinel-1 SAR overlay — Phase 4.x.
// Scenes = footprint polygons (semi-transparent fill, color by state).
// Detections = vessel returns from the CFAR detector. Red ≈ dark-vessel
// candidate (no AIS within fusion window when scene was processed),
// green ≈ AIS-matched. matched_entity_id is set by the fusion engine
// post-detection.
const SAR_SCENES_SOURCE_ID = "ss-sar-scenes";
const SAR_SCENES_FILL_LAYER_ID = "ss-sar-scenes-fill";
const SAR_SCENES_LINE_LAYER_ID = "ss-sar-scenes-line";
const SAR_DETECTIONS_SOURCE_ID = "ss-sar-detections";
const SAR_DETECTIONS_LAYER_ID = "ss-sar-detections-circle";
const SAR_STORAGE_KEY = "ss-sar-overlay";

// Cross-scene SAR track polyline — drawn when a dark-vessel detection
// click reveals an entity that's been detected in 2+ scenes (engine
// links via fusion._best_dark_vessel_match within 12 km / 90 min).
const SAR_TRACK_SOURCE_ID = "ss-sar-track";
const SAR_TRACK_LINE_LAYER_ID = "ss-sar-track-line";
const SAR_TRACK_POINT_LAYER_ID = "ss-sar-track-point";

const SAR_SCENES_PATH = "/maritime/sar/scenes?limit=200";
const SAR_DETECTIONS_PATH = "/maritime/sar/detections?limit=5000";

// Sentinel-2 optical overlay — Phase 4.x sensor-stack expansion.
// Catalog only for now (Phase 4.y will add per-detection RGB chips).
// Footprints are rendered separate from SAR with a cyan tint so the
// operator can quickly tell whether there is a recent daylight pass
// over the area being inspected.
const S2_SCENES_SOURCE_ID = "ss-s2-scenes";
const S2_SCENES_FILL_LAYER_ID = "ss-s2-scenes-fill";
const S2_SCENES_LINE_LAYER_ID = "ss-s2-scenes-line";
const S2_STORAGE_KEY = "ss-s2-overlay";
const S2_SCENES_PATH = "/maritime/s2/scenes?limit=200&since_hours=168&max_cloud=40";

// NDBC buoys — real-time ocean weather observations from NOAA's
// National Data Buoy Center. ~30-min cadence per station. Our seven
// AOI buoys cover the Texas Gulf shelf from Brownsville to Pensacola.
const BUOYS_SOURCE_ID = "ss-buoys";
const BUOYS_LAYER_ID = "ss-buoys-circle";
const BUOYS_STORAGE_KEY = "ss-buoys-overlay";
const BUOYS_PATH = "/maritime/buoys";
// Auto-refresh cadence — buoys only update every ~30 min so polling
// every 5 min is plenty conservative; cuts client bandwidth without
// losing freshness.
const BUOYS_REFRESH_MS = 5 * 60 * 1000;

// GOES-East ABI real-time imagery via NASA GIBS WMTS, ~10 min cadence.
// Three modes (cycled by a single button):
//   off       — layer hidden
//   geocolor  — daytime true-color (clouds, sun glint, ocean)
//   firetemp  — thermal-fire-tuned: bright orange/red over hot anomalies
//               (ship engine fires, refinery flares, wildfires)
// Single source with setTiles() swap on mode change. Max zoom 7 — GIBS
// limit for these products.
const GOES_SOURCE_ID = "ss-goes";
const GOES_LAYER_ID = "ss-goes-raster";
const GOES_STORAGE_KEY = "ss-goes-mode";   // "off" | "geocolor" | "firetemp"
const GOES_TILE_URLS = {
  geocolor: "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/" +
            "GOES-East_ABI_GeoColor/default/default/" +
            "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png",
  firetemp: "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/" +
            "GOES-East_ABI_FireTemp/default/default/" +
            "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png",
};
const GOES_REFRESH_MS = 10 * 60 * 1000;
const GOES_OPACITY = 0.55;
const GOES_CYCLE = ["off", "geocolor", "firetemp"];

// VIIRS — polar-orbiter day/night band via GIBS, ~12 h revisit per
// satellite. The "Enhanced Near Constant Contrast" recipe stretches
// nighttime radiance so lit ships pop against dark ocean — same
// imagery NOAA EOG runs their VIIRS Boat Detection (VBD) algorithm
// against. Operator gets the visual; point-level VBD detections
// require EOG registration and would land in a separate vbd_detections
// table fed via Observation(source=VIIRS) — Phase 5.
const VIIRS_SOURCE_ID = "ss-viirs";
const VIIRS_LAYER_ID = "ss-viirs-raster";
const VIIRS_STORAGE_KEY = "ss-viirs-mode";   // "off" | "dnb"
const VIIRS_TILE_URLS = {
  dnb: "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/" +
       "VIIRS_SNPP_DayNightBand_ENCC/default/default/" +
       "GoogleMapsCompatible_Level8/{z}/{y}/{x}.png",
};
const VIIRS_REFRESH_MS = 30 * 60 * 1000;     // 12 h cadence; poll cheap
const VIIRS_OPACITY = 0.7;
const VIIRS_CYCLE = ["off", "dnb"];

async function fetchTrack(apiPath, eid, signal) {
  const r = await fetch(`${API_BASE}${apiPath}/entities/${eid}/track?limit=200`, { signal });
  if (!r.ok) throw new Error(`track ${r.status}`);
  return r.json();
}

async function fetchSarOverlay(signal) {
  // /maritime is hard-coded — SAR is maritime-only for now (wildfire
  // would have its own scene catalog if/when we add VIIRS/GOES tiling).
  const [scenesR, detectionsR] = await Promise.all([
    fetch(`${API_BASE}${SAR_SCENES_PATH}`, { signal }),
    fetch(`${API_BASE}${SAR_DETECTIONS_PATH}`, { signal }),
  ]);
  if (!scenesR.ok) throw new Error(`sar scenes ${scenesR.status}`);
  if (!detectionsR.ok) throw new Error(`sar detections ${detectionsR.status}`);
  const [scenes, detections] = await Promise.all([scenesR.json(), detectionsR.json()]);
  return { scenes, detections };
}

async function fetchS2Overlay(signal) {
  const r = await fetch(`${API_BASE}${S2_SCENES_PATH}`, { signal });
  if (!r.ok) throw new Error(`s2 scenes ${r.status}`);
  return r.json();
}

async function fetchBuoys(signal) {
  const r = await fetch(`${API_BASE}${BUOYS_PATH}`, { signal });
  if (!r.ok) throw new Error(`buoys ${r.status}`);
  return r.json();
}

async function fetchTimeline(apiPath, atIso, signal) {
  const url = `${API_BASE}${apiPath}/timeline?at=${encodeURIComponent(atIso)}&lookback_minutes=60`;
  const r = await fetch(url, { signal });
  if (!r.ok) throw new Error(`timeline ${r.status}`);
  return r.json();
}

// Map a /timeline `snapshot` row to the entity shape Workbench feeds the map.
// /timeline is intentionally lightweight and doesn't include obs IDs / tracks.
function snapshotToEntity(s) {
  return {
    id: s.entity_id,
    type: s.type,
    lon: s.lon,
    lat: s.lat,
    priority: s.priority_score ?? 0.5,
    confidence: 1.0,
    name: s.name,
    mmsi: s.mmsi,
    attrs: { mmsi: s.mmsi, name: s.name },
    first_seen: s.t,
    last_seen: s.t,
    notes: "",
    obs_count: 0,
    track: [],
    obs: [],
    recommendation: null,
  };
}

function entityRadius(type) {
  if (type === "vessel" || type === "false_positive") return 4;
  if (type === "fire_event") return 7;
  return 6;
}

// Vessel-type SVG glyphs keyed off the AIS ship_type code (ITU-R
// M.1371). The shape rotates with the vessel's heading so the operator
// can see vessel class at a glance — "that's a tanker, not just any
// dot". Falls back to the generic circle when ship_type is missing
// (most non-Class-A transmitters) or for non-AIS entity types
// (dark_vessel, fire_event, etc).
//
// Shapes are designed inside a viewBox of about [-6, -8] to [6, 8] —
// bow points "up" (north) before rotation. Each glyph is filled with
// the type-meta color so the type/anomaly classification still reads.
function _vesselGlyph(shipType, color, stroke, strokeWidth) {
  if (shipType == null) return null;
  const n = Number(shipType);
  if (Number.isNaN(n)) return null;
  // Cargo (70-79): rectangular hull with three cargo containers on top.
  if (n >= 70 && n <= 79) {
    return `
      <path d="M -4 -7 L 4 -7 L 5 7 L -5 7 Z"
            fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"
            stroke-linejoin="round" />
      <rect x="-3" y="-5" width="6" height="2.2" fill="${stroke}" opacity="0.6"/>
      <rect x="-3" y="-2" width="6" height="2.2" fill="${stroke}" opacity="0.6"/>
      <rect x="-3" y="1"  width="6" height="2.2" fill="${stroke}" opacity="0.6"/>
    `;
  }
  // Tanker (80-89): rounded hull with a dome on the deck.
  if (n >= 80 && n <= 89) {
    return `
      <path d="M -4 -7 Q 0 -8 4 -7 L 4.5 6 Q 0 8 -4.5 6 Z"
            fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"
            stroke-linejoin="round" />
      <circle cx="0" cy="0" r="2.5" fill="${stroke}" opacity="0.55"/>
    `;
  }
  // Passenger / cruise (60-69): elongated hull with horizontal deck lines.
  if (n >= 60 && n <= 69) {
    return `
      <path d="M -3 -8 L 3 -8 L 3.5 7 L -3.5 7 Z"
            fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"
            stroke-linejoin="round" />
      <line x1="-3" y1="-3" x2="3" y2="-3" stroke="${stroke}" stroke-width="0.6" opacity="0.7"/>
      <line x1="-3" y1="0"  x2="3" y2="0"  stroke="${stroke}" stroke-width="0.6" opacity="0.7"/>
      <line x1="-3" y1="3"  x2="3" y2="3"  stroke="${stroke}" stroke-width="0.6" opacity="0.7"/>
    `;
  }
  // Fishing (30) — small hull plus a vertical mast.
  if (n === 30) {
    return `
      <path d="M -3 -3 L 3 -3 L 3.5 5 L -3.5 5 Z"
            fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"
            stroke-linejoin="round" />
      <line x1="0" y1="-3" x2="0" y2="-9" stroke="${color}" stroke-width="1.2"/>
      <line x1="-2.5" y1="-7" x2="2.5" y2="-7" stroke="${color}" stroke-width="0.8"/>
    `;
  }
  // Sailing / pleasure (36-37): triangle sail.
  if (n === 36 || n === 37) {
    return `
      <path d="M 0 -8 L -3 5 L 3 5 Z"
            fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"
            stroke-linejoin="round" />
      <path d="M -4 5 Q 0 7 4 5 Z"
            fill="${color}" opacity="0.7"
            stroke="${stroke}" stroke-width="${strokeWidth}"/>
    `;
  }
  // Tug / SAR / pilot / port-service (50-59): chunky square.
  if (n >= 50 && n <= 59) {
    return `
      <path d="M -3.5 -5 L 3.5 -5 L 3 6 L -3 6 Z"
            fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"
            stroke-linejoin="round" />
      <rect x="-1.5" y="-2.5" width="3" height="3" fill="${stroke}" opacity="0.55"/>
    `;
  }
  // Anything else AIS reports (high-speed craft 40-49, military 35,
  // dredging 33, towing 31-32, other 90-99): generic boat silhouette.
  if (n >= 1 && n <= 99) {
    return `
      <path d="M 0 -7 L 4 -2 L 3 6 L -3 6 L -4 -2 Z"
            fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"
            stroke-linejoin="round" />
    `;
  }
  return null;
}

// Anomaly transitions worth alerting on. A vessel newly entering any
// of these states gets a toast (and dark_vessel gets a sound).
const ANOMALY_ALERT_TYPES = new Set([
  "dark_vessel", "ais_spoofed", "loitering_vessel", "ais_gap", "port_skipping",
]);

// localStorage keys for user preferences.
const SOUND_STORAGE_KEY = "ss-sound-alerts";
const TOAST_TTL_MS = 6000;
const TOAST_MAX_VISIBLE = 5;

// Web Audio synth ping for new dark-vessel detections. Two short tones
// (660 Hz → 880 Hz) over ~250 ms — distinct enough to register over
// ambient room noise, subtle enough that the operator isn't ducking.
// Lazily-instantiated AudioContext (browsers require a user gesture
// to create one; we defer until the first ping attempt).
let _audioCtx = null;
function _playDarkVesselPing() {
  try {
    if (!_audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      _audioCtx = new Ctx();
    }
    const now = _audioCtx.currentTime;
    const tones = [
      { freq: 660, start: now + 0.0, duration: 0.12 },
      { freq: 880, start: now + 0.14, duration: 0.14 },
    ];
    for (const t of tones) {
      const osc = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = t.freq;
      gain.gain.setValueAtTime(0.0001, t.start);
      gain.gain.exponentialRampToValueAtTime(0.18, t.start + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t.start + t.duration);
      osc.connect(gain).connect(_audioCtx.destination);
      osc.start(t.start);
      osc.stop(t.start + t.duration + 0.01);
    }
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("dark-vessel ping failed:", err);
  }
}

export default function MapLibreView({ entities, selectedId, pinnedId, onSelect, cfg }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef(new Map());      // id → maplibregl.Marker
  const fitDoneRef = useRef(false);          // only auto-fit once on first data
  const [ready, setReady] = useState(false);

  // Anomaly state tracker. Per-entity-id record of the LAST observed
  // type, so we can detect transitions (vessel → dark_vessel, etc) on
  // every entities prop change without re-firing on stable state.
  // First pass is special-cased: we initialize the map silently so we
  // don't toast every dark vessel that was already present at load.
  const lastTypeRef = useRef(new Map());
  const initializedRef = useRef(false);
  const [toasts, setToasts] = useState([]);
  const [soundEnabled, setSoundEnabled] = useState(() => {
    if (typeof window === "undefined") return true;
    try {
      const v = window.localStorage.getItem(SOUND_STORAGE_KEY);
      return v === null ? true : v === "1";
    } catch {
      return true;
    }
  });
  const persistSound = (on) => {
    setSoundEnabled(on);
    try { window.localStorage.setItem(SOUND_STORAGE_KEY, on ? "1" : "0"); }
    catch { /* localStorage may be disabled */ }
  };

  // Anomaly trail window — operator picks how far back the per-anomaly
  // trail polylines extend. Persists in localStorage so a demo doesn't
  // have to re-pick it after a reload.
  const [trailWindow, setTrailWindow] = useState(() => {
    if (typeof window === "undefined") return "1h";
    try {
      const v = window.localStorage.getItem("ss-trail-window");
      return ["1h", "6h", "24h"].includes(v) ? v : "1h";
    } catch { return "1h"; }
  });
  const persistTrailWindow = (w) => {
    setTrailWindow(w);
    try { window.localStorage.setItem("ss-trail-window", w); } catch {}
  };
  const trailWindowMs = ({
    "1h": 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
  })[trailWindow] || 60 * 60 * 1000;

  const [heatmapOn, setHeatmapOn] = useState(() => {
    if (typeof window === "undefined") return false;
    try { return window.localStorage.getItem(HEATMAP_STORAGE_KEY) === "1"; }
    catch { return false; }
  });
  const persistHeatmap = (on) => {
    setHeatmapOn(on);
    try { window.localStorage.setItem(HEATMAP_STORAGE_KEY, on ? "1" : "0"); }
    catch {}
  };
  const [workloadOn, setWorkloadOn] = useState(() => {
    if (typeof window === "undefined") return false;
    try { return window.localStorage.getItem(WORKLOAD_STORAGE_KEY) === "1"; }
    catch { return false; }
  });
  const persistWorkload = (on) => {
    setWorkloadOn(on);
    try { window.localStorage.setItem(WORKLOAD_STORAGE_KEY, on ? "1" : "0"); }
    catch {}
  };
  const [assetsOn, setAssetsOn] = useState(() => {
    if (typeof window === "undefined") return false;
    try { return window.localStorage.getItem(ASSETS_STORAGE_KEY) === "1"; }
    catch { return false; }
  });
  const persistAssets = (on) => {
    setAssetsOn(on);
    try { window.localStorage.setItem(ASSETS_STORAGE_KEY, on ? "1" : "0"); }
    catch {}
  };

  // Active dispatches — { entity_id, dispatched_at_ms, action_type }[].
  // Loaded from localStorage so a reload doesn't lose tracking. The
  // DispatchSection component (in Workbench.jsx) dispatches a
  // `ss-dispatch-filed` window event after a successful POST; we
  // listen for it here and append.
  const [activeDispatches, setActiveDispatches] = useState(() => {
    if (typeof window === "undefined") return [];
    try {
      const raw = window.localStorage.getItem(ACTIVE_DISPATCH_STORAGE_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      const cutoff = Date.now() - ACTIVE_DISPATCH_TTL_MS;
      return parsed.filter((d) => d.dispatched_at_ms > cutoff);
    } catch { return []; }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(
        ACTIVE_DISPATCH_STORAGE_KEY, JSON.stringify(activeDispatches),
      );
    } catch {}
  }, [activeDispatches]);

  useEffect(() => {
    const handler = (e) => {
      const d = e.detail || {};
      if (!d.entity_id) return;
      setActiveDispatches((cur) => {
        // Dedupe — refile of same entity replaces the timestamp.
        const filtered = cur.filter((x) => x.entity_id !== d.entity_id);
        return [...filtered, {
          entity_id: d.entity_id,
          dispatched_at_ms: Date.now(),
          action_type: d.action_type || "log_only",
          audit_seq: d.audit_seq,
        }];
      });
    };
    window.addEventListener("ss-dispatch-filed", handler);
    return () => window.removeEventListener("ss-dispatch-filed", handler);
  }, []);

  // Periodic TTL sweep — evict expired dispatches every 60 s.
  useEffect(() => {
    const id = window.setInterval(() => {
      const cutoff = Date.now() - ACTIVE_DISPATCH_TTL_MS;
      setActiveDispatches((cur) => cur.filter((d) => d.dispatched_at_ms > cutoff));
    }, 60_000);
    return () => window.clearInterval(id);
  }, []);

  // AOI drawing state.
  //   aoiMode: 'idle' | 'drawing' | 'fixed'
  //   aoiPoints: [[lon,lat], ...] — closed polygon when in 'fixed' mode,
  //              in-progress vertex list when 'drawing'.
  const [aoiMode, setAoiMode] = useState("idle");
  const [aoiPoints, setAoiPoints] = useState([]);

  // Saved AOI presets — operator can name + save a drawn polygon and
  // recall it instantly. Stored in localStorage as [{id, name,
  // points, created_at}]. Limit 12 entries to keep the picker manageable.
  const SAVED_AOI_STORAGE_KEY = "ss-saved-aois";
  const SAVED_AOI_MAX = 12;
  const [savedAois, setSavedAois] = useState(() => {
    if (typeof window === "undefined") return [];
    try {
      const raw = window.localStorage.getItem(SAVED_AOI_STORAGE_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch { return []; }
  });
  const persistSavedAois = (next) => {
    setSavedAois(next);
    try {
      window.localStorage.setItem(SAVED_AOI_STORAGE_KEY, JSON.stringify(next));
    } catch {}
  };
  const [savedAoisPickerOpen, setSavedAoisPickerOpen] = useState(false);
  // Ref mirrors so map event listeners (set up once) can read fresh state.
  const aoiModeRef = useRef(aoiMode);
  const aoiPointsRef = useRef(aoiPoints);
  useEffect(() => { aoiModeRef.current = aoiMode; }, [aoiMode]);
  useEffect(() => { aoiPointsRef.current = aoiPoints; }, [aoiPoints]);

  // Basemap pick — persisted across sessions.
  const [basemap, setBasemap] = useState(() => {
    if (typeof window === "undefined") return DEFAULT_BASEMAP;
    try {
      const saved = window.localStorage.getItem(BASEMAP_STORAGE_KEY);
      return saved && BASEMAPS[saved] ? saved : DEFAULT_BASEMAP;
    } catch {
      return DEFAULT_BASEMAP;
    }
  });

  // Time-scrub state. scrubMinutes==0 means "live / now"; positive values
  // step backward in time (T-1, T-2, ... T-60). When scrubbing we hide the
  // live `entities` prop and render a snapshot from /maritime/timeline.
  const [scrubMinutes, setScrubMinutes] = useState(0);
  const [scrubSnapshot, setScrubSnapshot] = useState(null);
  const [scrubLoading, setScrubLoading] = useState(false);

  // Raw track for the selected entity (full Postgres history). Re-derived
  // into GeoJSON features whenever scrubMinutes changes so the polyline
  // clips correctly to the scrubbed-to instant.
  const [trackPoints, setTrackPoints] = useState([]);

  // SAR overlay toggle — persisted across sessions. Default off so first-load
  // operators see the AIS map clean; flip on to overlay scene footprints +
  // detections.
  const [showSar, setShowSar] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.localStorage.getItem(SAR_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [sarData, setSarData] = useState(null);   // { scenes, detections } or null
  const [showS2, setShowS2] = useState(() => {
    if (typeof window === "undefined") return false;
    try { return window.localStorage.getItem(S2_STORAGE_KEY) === "1"; }
    catch { return false; }
  });
  const [s2Data, setS2Data] = useState(null);     // FeatureCollection or null

  const [showBuoys, setShowBuoys] = useState(() => {
    if (typeof window === "undefined") return true;     // default ON for the demo
    try {
      const raw = window.localStorage.getItem(BUOYS_STORAGE_KEY);
      return raw == null ? true : raw === "1";
    } catch { return true; }
  });
  const [buoysData, setBuoysData] = useState(null);

  const [goesMode, setGoesMode] = useState(() => {
    if (typeof window === "undefined") return "off";
    try {
      const v = window.localStorage.getItem(GOES_STORAGE_KEY);
      return GOES_CYCLE.includes(v) ? v : "off";
    } catch { return "off"; }
  });

  const [viirsMode, setViirsMode] = useState(() => {
    if (typeof window === "undefined") return "off";
    try {
      const v = window.localStorage.getItem(VIIRS_STORAGE_KEY);
      return VIIRS_CYCLE.includes(v) ? v : "off";
    } catch { return "off"; }
  });

  // ------------------------------------------------------------------
  // Initialize the map exactly once.
  // ------------------------------------------------------------------
  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: BASEMAPS[basemap].style,
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
      attributionControl: { compact: true },
      // Wheel zoom but no rotation/pitch — operators don't need 3D.
      pitchWithRotate: false,
      dragRotate: false,
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "nautical" }), "bottom-left");

    // setStyle() wipes all sources/layers, so re-attach them whenever a
    // new style finishes loading. Same code path used on initial load.
    const ensureTrackLayers = () => {
      if (map.getSource(TRACK_SOURCE_ID)) return;
      map.addSource(TRACK_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: TRACK_LAYER_ID,
        type: "line",
        source: TRACK_SOURCE_ID,
        filter: ["==", "$type", "LineString"],
        paint: {
          "line-color": "#5fd093",
          "line-width": 2,
          "line-opacity": 0.85,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });
      map.addLayer({
        id: TRACK_HEAD_LAYER_ID,
        type: "circle",
        source: TRACK_SOURCE_ID,
        filter: ["==", "$type", "Point"],
        paint: {
          "circle-color": "#5fd093",
          "circle-radius": 2.5,
          "circle-opacity": 0.7,
          "circle-stroke-color": "#040810",
          "circle-stroke-width": 0.5,
        },
      });
    };

    // Candidate-hull halos — yellow rings around the AIS-cooperative
    // vessels within 5 km of the selected anomaly. One ring layer + one
    // pulse layer (the pulse is animated via setPaintProperty in a
    // separate effect, but a static fallback ring is drawn so the halo
    // is still visible if the animation never runs).
    const ensureCandidateLayers = () => {
      if (map.getSource(CANDIDATES_SOURCE_ID)) return;
      map.addSource(CANDIDATES_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      // Outer pulse ring — larger, animated opacity. Sits BELOW the
      // sharp inner ring so the pulse glow doesn't wash out the edge.
      map.addLayer({
        id: CANDIDATES_PULSE_LAYER_ID,
        type: "circle",
        source: CANDIDATES_SOURCE_ID,
        paint: {
          "circle-color": "rgba(0,0,0,0)",
          "circle-radius": 20,
          "circle-stroke-color": "#f0c930",
          "circle-stroke-width": 1.0,
          "circle-stroke-opacity": 0.35,
        },
      });
      // Inner sharp ring — fixed.
      map.addLayer({
        id: CANDIDATES_LAYER_ID,
        type: "circle",
        source: CANDIDATES_SOURCE_ID,
        paint: {
          "circle-color": "rgba(0,0,0,0)",
          "circle-radius": 11,
          "circle-stroke-color": "#f0c930",
          "circle-stroke-width": 2,
          "circle-stroke-opacity": 0.9,
        },
      });
    };

    // SAR scene footprints + detections. Same lifecycle as the track
    // layers — re-attached on every style.load (including basemap swaps).
    const ensureSarLayers = () => {
      if (!map.getSource(SAR_SCENES_SOURCE_ID)) {
        map.addSource(SAR_SCENES_SOURCE_ID, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
        // Fill is colored by scene state. 'detected' (CFAR ran) is muted
        // amber so it stands out against the dark/satellite basemap;
        // 'discovered' is a colder blue to telegraph "catalog only";
        // 'failed' goes red so ops can spot busted scenes at a glance.
        map.addLayer({
          id: SAR_SCENES_FILL_LAYER_ID,
          type: "fill",
          source: SAR_SCENES_SOURCE_ID,
          paint: {
            "fill-color": [
              "match",
              ["get", "state"],
              "detected", "#f0a830",
              "downloaded", "#5fd093",
              "discovered", "#5fa8d0",
              "failed", "#e0556e",
              "#888888",
            ],
            "fill-opacity": 0.10,
          },
        });
        map.addLayer({
          id: SAR_SCENES_LINE_LAYER_ID,
          type: "line",
          source: SAR_SCENES_SOURCE_ID,
          paint: {
            "line-color": [
              "match",
              ["get", "state"],
              "detected", "#f0a830",
              "downloaded", "#5fd093",
              "discovered", "#5fa8d0",
              "failed", "#e0556e",
              "#888888",
            ],
            "line-width": 1.3,
            "line-opacity": 0.65,
          },
        });
      }
      if (!map.getSource(S2_SCENES_SOURCE_ID)) {
        map.addSource(S2_SCENES_SOURCE_ID, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
        // S2 scenes: cyan tint, semi-transparent, lower opacity than
        // SAR so the two can stack visibly when both layers are on.
        map.addLayer({
          id: S2_SCENES_FILL_LAYER_ID,
          type: "fill",
          source: S2_SCENES_SOURCE_ID,
          paint: {
            "fill-color": "#5fbed0",
            "fill-opacity": 0.07,
          },
        });
        map.addLayer({
          id: S2_SCENES_LINE_LAYER_ID,
          type: "line",
          source: S2_SCENES_SOURCE_ID,
          paint: {
            "line-color": "#5fbed0",
            "line-width": 1.0,
            "line-opacity": 0.55,
            "line-dasharray": [3, 2],
          },
        });
      }
      if (!map.getSource(GOES_SOURCE_ID)) {
        map.addSource(GOES_SOURCE_ID, {
          type: "raster",
          tiles: [GOES_TILE_URLS.geocolor],   // mode-effect swaps via setTiles
          tileSize: 256,
          attribution:
            "GOES-East &copy; NOAA via NASA GIBS",
          maxzoom: 7,
        });
        map.addLayer({
          id: GOES_LAYER_ID,
          type: "raster",
          source: GOES_SOURCE_ID,
          paint: { "raster-opacity": GOES_OPACITY },
          layout: { "visibility": "none" },   // mode-effect makes it visible
        });
      }
      if (!map.getSource(SAR_TRACK_SOURCE_ID)) {
        map.addSource(SAR_TRACK_SOURCE_ID, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
        // Dashed red line through multi-pass dark-vessel positions —
        // matches the dark-vessel red dot color so the relationship
        // is visually obvious.
        map.addLayer({
          id: SAR_TRACK_LINE_LAYER_ID,
          type: "line",
          source: SAR_TRACK_SOURCE_ID,
          filter: ["==", "$type", "LineString"],
          paint: {
            "line-color": "#e0556e",
            "line-width": 1.8,
            "line-opacity": 0.9,
            "line-dasharray": [3, 2],
          },
          layout: { "line-cap": "round", "line-join": "round" },
        });
        map.addLayer({
          id: SAR_TRACK_POINT_LAYER_ID,
          type: "circle",
          source: SAR_TRACK_SOURCE_ID,
          filter: ["==", "$type", "Point"],
          paint: {
            "circle-color": "#e0556e",
            "circle-radius": 3,
            "circle-stroke-color": "#040810",
            "circle-stroke-width": 1,
          },
        });
      }
      if (!map.getSource(VIIRS_SOURCE_ID)) {
        map.addSource(VIIRS_SOURCE_ID, {
          type: "raster",
          tiles: [VIIRS_TILE_URLS.dnb],   // mode-effect swaps via setTiles
          tileSize: 256,
          attribution: "VIIRS &copy; NOAA / NASA via GIBS",
          maxzoom: 8,
        });
        map.addLayer({
          id: VIIRS_LAYER_ID,
          type: "raster",
          source: VIIRS_SOURCE_ID,
          paint: { "raster-opacity": VIIRS_OPACITY },
          layout: { "visibility": "none" },
        });
      }
      if (!map.getSource(BUOYS_SOURCE_ID)) {
        map.addSource(BUOYS_SOURCE_ID, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
        map.addLayer({
          id: BUOYS_LAYER_ID,
          type: "circle",
          source: BUOYS_SOURCE_ID,
          paint: {
            // Buoys with no recent observation get a desaturated color.
            "circle-color": [
              "case",
              ["==", ["get", "observation"], null], "#888",
              "#5fa8d0",
            ],
            "circle-radius": 6,
            "circle-stroke-color": "#040810",
            "circle-stroke-width": 1.2,
            "circle-opacity": 0.9,
          },
        });
      }
      if (!map.getSource(SAR_DETECTIONS_SOURCE_ID)) {
        map.addSource(SAR_DETECTIONS_SOURCE_ID, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
        // Detection points: red square-ish marker for dark-vessel
        // candidates (matched_entity_id is null), green diamond-ish for
        // AIS-matched. Sized small (4 px) so a few hundred per scene
        // don't cover the AIS markers.
        map.addLayer({
          id: SAR_DETECTIONS_LAYER_ID,
          type: "circle",
          source: SAR_DETECTIONS_SOURCE_ID,
          paint: {
            "circle-color": [
              "case",
              ["==", ["get", "matched_entity_id"], null], "#e0556e",
              "#5fd093",
            ],
            "circle-radius": 4,
            "circle-stroke-color": "#040810",
            "circle-stroke-width": 1,
            "circle-opacity": 0.85,
          },
        });
      }
    };
    // Convoy lines — segments connecting vessels with the same
    // backend-tagged convoy_id. Rendered as a thin cyan line + a
    // matching circle at each member position so the formation reads
    // even when zoomed all the way out.
    const ensureConvoyLayers = () => {
      if (map.getSource(CONVOY_SOURCE_ID)) return;
      map.addSource(CONVOY_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: CONVOY_LINE_LAYER_ID,
        type: "line",
        source: CONVOY_SOURCE_ID,
        filter: ["==", "$type", "LineString"],
        paint: {
          "line-color": "#5dd6c4",
          "line-width": 1.2,
          "line-opacity": 0.65,
          "line-dasharray": [3, 2],
        },
      });
      map.addLayer({
        id: CONVOY_HULL_LAYER_ID,
        type: "circle",
        source: CONVOY_SOURCE_ID,
        filter: ["==", "$type", "Point"],
        paint: {
          "circle-color": "rgba(0,0,0,0)",
          "circle-radius": 9,
          "circle-stroke-color": "#5dd6c4",
          "circle-stroke-width": 1.2,
          "circle-stroke-opacity": 0.8,
        },
      });
    };

    // AOI draw layers — a fill + outline for the polygon and a circle
    // layer for the vertices the operator has clicked so far.
    const ensureAoiLayers = () => {
      if (map.getSource(AOI_DRAW_SOURCE_ID)) return;
      map.addSource(AOI_DRAW_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: AOI_DRAW_FILL_LAYER_ID,
        type: "fill",
        source: AOI_DRAW_SOURCE_ID,
        filter: ["==", "$type", "Polygon"],
        paint: {
          "fill-color": "#f0c930",
          "fill-opacity": 0.08,
        },
      });
      map.addLayer({
        id: AOI_DRAW_LINE_LAYER_ID,
        type: "line",
        source: AOI_DRAW_SOURCE_ID,
        filter: ["any",
          ["==", "$type", "Polygon"],
          ["==", "$type", "LineString"],
        ],
        paint: {
          "line-color": "#f0c930",
          "line-width": 1.5,
          "line-dasharray": [2, 2],
        },
      });
      map.addLayer({
        id: AOI_DRAW_VERTEX_LAYER_ID,
        type: "circle",
        source: AOI_DRAW_SOURCE_ID,
        filter: ["==", "$type", "Point"],
        paint: {
          "circle-color": "#040810",
          "circle-radius": 4,
          "circle-stroke-color": "#f0c930",
          "circle-stroke-width": 1.5,
        },
      });
    };

    // Anomaly trails — last-hour polyline per anomaly entity. Color
    // and opacity come from per-feature properties so each trail can
    // be tinted to match its entity's typeMeta on the marker layer.
    const ensureAnomalyTrailsLayers = () => {
      if (map.getSource(ANOMALY_TRAILS_SOURCE_ID)) return;
      map.addSource(ANOMALY_TRAILS_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: ANOMALY_TRAILS_LAYER_ID,
        type: "line",
        source: ANOMALY_TRAILS_SOURCE_ID,
        filter: ["==", "$type", "LineString"],
        paint: {
          "line-color": ["get", "color"],
          "line-width": 1.6,
          "line-opacity": 0.55,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });
    };

    // Activity heatmap — kernel-density layer over the current
    // vessel positions. Heatmap intensity is per-point, dropping off
    // smoothly so the result reads as "where the traffic is right
    // now". We render it UNDER the markers so dots remain readable.
    const ensureHeatmapLayers = () => {
      if (map.getSource(HEATMAP_SOURCE_ID)) return;
      map.addSource(HEATMAP_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: HEATMAP_LAYER_ID,
        type: "heatmap",
        source: HEATMAP_SOURCE_ID,
        layout: { visibility: "none" },
        paint: {
          "heatmap-weight": 1,
          "heatmap-intensity": [
            "interpolate", ["linear"], ["zoom"],
            5, 0.6,
            12, 2.2,
          ],
          "heatmap-radius": [
            "interpolate", ["linear"], ["zoom"],
            5, 12,
            12, 30,
          ],
          "heatmap-opacity": 0.6,
          "heatmap-color": [
            "interpolate", ["linear"], ["heatmap-density"],
            0,    "rgba(0,0,0,0)",
            0.1,  "rgba(82,113,196,0.55)",
            0.35, "rgba(95,208,147,0.7)",
            0.6,  "rgba(240,168,48,0.8)",
            0.85, "rgba(224,85,110,0.85)",
            1,    "rgba(255,255,255,0.9)",
          ],
        },
      });
    };

    // Pinned-track layer — amber-tinted twin of TRACK_LAYER for the
    // pinned comparison entity. Drawn the same way so the operator
    // can compare two trajectories side by side.
    const ensurePinnedTrackLayers = () => {
      if (map.getSource(PINNED_TRACK_SOURCE_ID)) return;
      map.addSource(PINNED_TRACK_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: PINNED_TRACK_LAYER_ID,
        type: "line",
        source: PINNED_TRACK_SOURCE_ID,
        filter: ["==", "$type", "LineString"],
        paint: {
          "line-color": PINNED_TRACK_COLOR,
          "line-width": 2,
          "line-opacity": 0.85,
          "line-dasharray": [4, 2],
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });
      map.addLayer({
        id: PINNED_HEAD_LAYER_ID,
        type: "circle",
        source: PINNED_TRACK_SOURCE_ID,
        filter: ["==", "$type", "Point"],
        paint: {
          "circle-color": PINNED_TRACK_COLOR,
          "circle-radius": 2.5,
          "circle-opacity": 0.85,
          "circle-stroke-color": "#040810",
          "circle-stroke-width": 0.5,
        },
      });
    };

    map.on("style.load", ensureTrackLayers);
    map.on("style.load", ensurePinnedTrackLayers);
    map.on("style.load", ensureCandidateLayers);
    map.on("style.load", ensureConvoyLayers);
    map.on("style.load", ensureAoiLayers);
    map.on("style.load", ensureAnomalyTrailsLayers);
    // Operator-workload heatmap — same approach as the activity
    // heatmap but on dispatch positions, with a distinct
    // purple→pink palette so the two can be on simultaneously
    // without visual confusion.
    const ensureWorkloadLayers = () => {
      if (map.getSource(WORKLOAD_SOURCE_ID)) return;
      map.addSource(WORKLOAD_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: WORKLOAD_LAYER_ID,
        type: "heatmap",
        source: WORKLOAD_SOURCE_ID,
        layout: { visibility: "none" },
        paint: {
          "heatmap-weight": 1,
          "heatmap-intensity": [
            "interpolate", ["linear"], ["zoom"],
            5, 0.8,
            12, 2.6,
          ],
          "heatmap-radius": [
            "interpolate", ["linear"], ["zoom"],
            5, 14,
            12, 36,
          ],
          "heatmap-opacity": 0.65,
          "heatmap-color": [
            "interpolate", ["linear"], ["heatmap-density"],
            0,    "rgba(0,0,0,0)",
            0.15, "rgba(95,70,150,0.5)",
            0.45, "rgba(170,80,200,0.75)",
            0.75, "rgba(255,90,210,0.85)",
            1,    "rgba(255,180,255,0.95)",
          ],
        },
      });
    };

    // Onshore assets — anchor icon for USCG, star for Navy. Visibility
    // toggled via the ASSETS chip; data fetched once on toggle-on.
    const ensureAssetLayers = () => {
      if (map.getSource(ASSETS_SOURCE_ID)) return;
      map.addSource(ASSETS_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: ASSETS_LAYER_ID,
        type: "circle",
        source: ASSETS_SOURCE_ID,
        layout: { visibility: "none" },
        paint: {
          "circle-color": [
            "match", ["get", "type"],
            "uscg", "#5fd093",     // USCG green
            "navy", "#7ab8ff",     // Navy blue
            "#a8a8b8",
          ],
          "circle-radius": 6,
          "circle-stroke-color": "#040810",
          "circle-stroke-width": 1.5,
          "circle-opacity": 0.95,
        },
      });
      map.addLayer({
        id: ASSETS_LABEL_LAYER_ID,
        type: "symbol",
        source: ASSETS_SOURCE_ID,
        layout: {
          visibility: "none",
          "text-field": ["get", "name"],
          "text-font": ["Open Sans Regular"],
          "text-size": 10,
          "text-anchor": "left",
          "text-offset": [0.8, 0],
          "text-allow-overlap": false,
        },
        paint: {
          "text-color": "#f0f4ff",
          "text-halo-color": "#040810",
          "text-halo-width": 1.5,
        },
      });
    };

    // Active-dispatch rings — yellow ring + "DISPATCHED" label around
    // every entity the operator has filed a dispatch on in the last
    // 4 h. The ring uses a different style than candidate halos so
    // they don't visually conflict.
    const ensureActiveDispatchLayers = () => {
      if (map.getSource(ACTIVE_DISPATCH_SOURCE_ID)) return;
      map.addSource(ACTIVE_DISPATCH_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: ACTIVE_DISPATCH_RING_LAYER_ID,
        type: "circle",
        source: ACTIVE_DISPATCH_SOURCE_ID,
        paint: {
          "circle-color": "rgba(0,0,0,0)",
          "circle-radius": 16,
          "circle-stroke-color": "#ffd897",
          "circle-stroke-width": 2.5,
          "circle-stroke-opacity": 0.85,
        },
      });
      map.addLayer({
        id: ACTIVE_DISPATCH_LABEL_LAYER_ID,
        type: "symbol",
        source: ACTIVE_DISPATCH_SOURCE_ID,
        layout: {
          "text-field": ["get", "label"],
          "text-font": ["Open Sans Regular"],
          "text-size": 9.5,
          "text-anchor": "top",
          "text-offset": [0, 1.6],
          "text-allow-overlap": false,
          "text-letter-spacing": 0.08,
        },
        paint: {
          "text-color": "#ffd897",
          "text-halo-color": "#040810",
          "text-halo-width": 1.6,
        },
      });
    };

    map.on("style.load", ensureHeatmapLayers);
    map.on("style.load", ensureWorkloadLayers);
    map.on("style.load", ensureAssetLayers);
    map.on("style.load", ensureActiveDispatchLayers);
    map.on("style.load", ensureSarLayers);

    // Click → popup. Detections are densely packed so attach to the
    // detection layer specifically (otherwise the scene-fill layer
    // intercepts at low zoom and pops up the scene instead).
    const popupForDetection = (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties || {};
      const dark = p.matched_entity_id == null;
      // Cache-busting param ensures we don't see stale cached chips
      // across detection_id reuses (which shouldn't happen but is cheap).
      const chipUrl = `${API_BASE}/maritime/sar/detections/${p.detection_id}/optical_chip`;
      // Fetch and render the SAR-only multi-scene track for this
      // detection's entity, if one is linked. Frontend track source
      // is reset on next detection click / popup close.
      const trackSrc = map.getSource(SAR_TRACK_SOURCE_ID);
      const empty = { type: "FeatureCollection", features: [] };
      if (trackSrc) trackSrc.setData(empty);
      if (p.entity_id) {
        fetch(`${API_BASE}/maritime/sar/entities/${p.entity_id}/track?limit=50`)
          .then((r) => r.ok ? r.json() : null)
          .then((data) => {
            if (!data || !data.track || data.track.length < 2) return;
            const coords = data.track.map((t) => [t.lon, t.lat]);
            const features = [
              { type: "Feature", geometry: { type: "LineString", coordinates: coords },
                properties: {} },
              ...coords.map((c) => ({
                type: "Feature", geometry: { type: "Point", coordinates: c },
                properties: {},
              })),
            ];
            const src = map.getSource(SAR_TRACK_SOURCE_ID);
            if (src) src.setData({ type: "FeatureCollection", features });
          })
          .catch(() => { /* noop */ });
      }
      const html = `
        <div style="font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;
                    font-size:11px;letter-spacing:0.04em;color:#040810;
                    min-width:280px;">
          <div style="font-weight:bold;color:${dark ? "#a02030" : "#1a6a3a"};
                      text-transform:uppercase;margin-bottom:4px;">
            ${dark ? "Dark vessel candidate" : "AIS-matched detection"}
          </div>
          <div>RCS: ${Number(p.rcs_db).toFixed(1)} dB</div>
          <div>Length: ${Number(p.length_m).toFixed(0)} m</div>
          <div>Confidence: ${Number(p.confidence).toFixed(2)}</div>
          ${p.vv_vh_ratio_db != null
            ? `<div>VV/VH ratio: ${Number(p.vv_vh_ratio_db).toFixed(1)} dB</div>`
            : ""}
          ${p.matched_entity_id
            ? `<div>Match: <code>${p.matched_entity_id}</code></div>`
            : ""}
          <div style="margin-top:4px;color:#666;">scene ${(p.scene_id || "").slice(0, 8)}…</div>
          <div style="margin-top:8px;border-top:1px solid #ddd;padding-top:6px;">
            <div style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.06em;
                        margin-bottom:3px;">
              Sentinel-2 daylight chip
            </div>
            <img
              src="${chipUrl}"
              alt="Sentinel-2 optical chip"
              style="display:block;width:260px;height:auto;background:#222;
                     border:1px solid #ddd;border-radius:3px;"
              onerror="this.outerHTML='<div style=&quot;color:#999;font-size:10px;padding:8px;background:#f0f0f0;border-radius:3px;&quot;>No optical match (closest S2 not downloaded yet, or no clear-sky pass within ±3 days)</div>'"
            />
          </div>
        </div>
      `;
      new maplibregl.Popup({ closeButton: true, maxWidth: "320px" })
        .setLngLat(f.geometry.coordinates)
        .setHTML(html)
        .addTo(map);
    };
    const popupForScene = (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties || {};
      const html = `
        <div style="font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;
                    font-size:11px;letter-spacing:0.04em;color:#040810;
                    min-width:220px;">
          <div style="font-weight:bold;text-transform:uppercase;margin-bottom:4px;">
            Sentinel-1 ${p.platform || ""} · ${p.state || ""}
          </div>
          <div>${p.acquired_at?.replace("T", " ").slice(0, 16)} UTC</div>
          <div>Polarization: ${p.polarization || "—"}</div>
          ${p.n_detections != null
            ? `<div>Detections: ${p.n_detections}</div>`
            : ""}
          <div style="margin-top:4px;color:#666;">scene ${(p.scene_id || "").slice(0, 8)}…</div>
        </div>
      `;
      new maplibregl.Popup({ closeButton: true, maxWidth: "300px" })
        .setLngLat(e.lngLat)
        .setHTML(html)
        .addTo(map);
    };
    const popupForS2Scene = (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties || {};
      const cc = p.cloud_cover_pct == null ? "—" : `${Number(p.cloud_cover_pct).toFixed(0)}%`;
      const html = `
        <div style="font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;
                    font-size:11px;letter-spacing:0.04em;color:#040810;
                    min-width:240px;">
          <div style="font-weight:bold;text-transform:uppercase;margin-bottom:4px;
                      color:#1a5f6e;">
            Sentinel-2 ${p.platform || ""} · ${p.product_type || ""}
          </div>
          <div>${p.acquired_at?.replace("T", " ").slice(0, 16)} UTC</div>
          <div>Cloud cover: ${cc}</div>
          <div>State: ${p.state}</div>
          <div style="margin-top:4px;color:#666;">scene ${(p.scene_id || "").slice(0, 8)}…</div>
        </div>
      `;
      new maplibregl.Popup({ closeButton: true, maxWidth: "320px" })
        .setLngLat(e.lngLat)
        .setHTML(html)
        .addTo(map);
    };
    const popupForBuoy = (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties || {};
      // GeoJSON properties come back stringified — re-parse the obs object.
      let obs = p.observation;
      if (typeof obs === "string") {
        try { obs = JSON.parse(obs); } catch { obs = null; }
      }
      const fmt = (v, unit, dec=1) => v == null ? "—" : `${Number(v).toFixed(dec)} ${unit}`;
      const html = `
        <div style="font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;
                    font-size:11px;letter-spacing:0.04em;color:#040810;
                    min-width:240px;">
          <div style="font-weight:bold;text-transform:uppercase;margin-bottom:4px;
                      color:#1a5f6e;">
            NDBC ${p.station_id} · ${p.name}
          </div>
          ${obs ? `
            <div>${(obs.t || "").replace("T"," ").slice(0, 16)} UTC</div>
            <div>Wind: ${fmt(obs.wind_speed_kn,'kn')} (gust ${fmt(obs.wind_gust_kn,'kn')}) @ ${fmt(obs.wind_dir_deg,'°',0)}</div>
            <div>Wave: ${fmt(obs.wave_height_m,'m')} period ${fmt(obs.dom_period_s,'s',0)}</div>
            <div>Air: ${fmt(obs.air_temp_c,'°C')} | Water: ${fmt(obs.water_temp_c,'°C')}</div>
            <div>Pressure: ${fmt(obs.pressure_hpa,'hPa',1)}</div>
          ` : `<div style="color:#999;">no recent observation</div>`}
        </div>`;
      new maplibregl.Popup({ closeButton: true, maxWidth: "300px" })
        .setLngLat(f.geometry.coordinates)
        .setHTML(html)
        .addTo(map);
    };
    const popupForConvoy = (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties || {};
      let members = [];
      try {
        members = JSON.parse(p.members_json || "[]");
      } catch { members = []; }
      // Also focus the side panel on the convoy's head member so the
      // operator can drill into a real vessel rather than only seeing
      // the popup. The first MMSI sort order = the head (matches the
      // backend's convoy_id naming convention).
      if (members.length > 0 && members[0].id && onSelect) {
        onSelect(members[0].id);
      }
      const rows = members.map((m) => {
        const ident = m.name || (m.mmsi ? `MMSI ${m.mmsi}` : "unidentified");
        const speed = m.speed_kn != null
          ? `${Number(m.speed_kn).toFixed(1)} kn`
          : "—";
        const hdg = m.heading != null
          ? `${Math.round(Number(m.heading))}°`
          : "—";
        return `<div style="display:grid;grid-template-columns:1fr 70px 50px;
                gap:8px;padding:3px 0;border-top:1px solid #d8e1ec;">
                  <span style="overflow:hidden;text-overflow:ellipsis;
                               white-space:nowrap;">${ident}</span>
                  <span style="text-align:right;color:#555;">${speed}</span>
                  <span style="text-align:right;color:#555;">${hdg}</span>
                </div>`;
      }).join("");
      const html = `
        <div style="font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;
                    font-size:11px;letter-spacing:0.04em;color:#040810;
                    min-width:260px;">
          <div style="font-weight:bold;text-transform:uppercase;
                      margin-bottom:4px;color:#1a5f6e;">
            CONVOY · ${members.length} VESSELS IN FORMATION
          </div>
          <div style="color:#666;font-size:10px;margin-bottom:6px;">
            ${(p.convoy_id || "").replace("convoy_", "head MMSI ")}
          </div>
          <div style="display:grid;grid-template-columns:1fr 70px 50px;
                      gap:8px;font-size:9px;color:#999;text-transform:uppercase;
                      letter-spacing:0.1em;padding-bottom:2px;">
            <span>vessel</span><span style="text-align:right;">speed</span>
            <span style="text-align:right;">hdg</span>
          </div>
          ${rows}
          <div style="margin-top:8px;padding-top:6px;border-top:1px solid #1a5f6e;
                      color:#1a5f6e;font-weight:600;">
            Recommendation: track all members. Confirm formation intent
            via next SAR pass before tasking surface interdiction.
          </div>
        </div>`;
      new maplibregl.Popup({ closeButton: true, maxWidth: "340px" })
        .setLngLat(e.lngLat)
        .setHTML(html)
        .addTo(map);
    };

    // AOI draw interaction: hijack generic map clicks while drawing.
    // We attach to map.on("click") (not a specific layer) but bail out
    // immediately if aoiModeRef.current !== 'drawing'. Layer-specific
    // click handlers still run normally because they're more specific.
    map.on("click", (e) => {
      if (aoiModeRef.current !== "drawing") return;
      const pts = aoiPointsRef.current.slice();
      pts.push([e.lngLat.lng, e.lngLat.lat]);
      setAoiPoints(pts);
    });
    map.on("dblclick", (e) => {
      if (aoiModeRef.current !== "drawing") return;
      // Suppress MapLibre's default dblclick zoom while finalizing.
      e.preventDefault();
      const pts = aoiPointsRef.current.slice();
      // Last single-click might have just added the duplicate vertex —
      // ignore the trailing dblclick coordinate.
      if (pts.length < 3) {
        setAoiMode("idle");
        setAoiPoints([]);
        return;
      }
      setAoiMode("fixed");
    });

    const popupForAsset = (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties || {};
      const html = `
        <div style="font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;
                    font-size:11px;letter-spacing:0.04em;color:#040810;
                    min-width:220px;">
          <div style="font-weight:bold;text-transform:uppercase;
                      margin-bottom:4px;color:${p.type === 'navy' ? '#1a4f8e' : '#1a5f6e'};">
            ${p.type === 'navy' ? 'US NAVY' : 'US COAST GUARD'}
          </div>
          <div style="font-weight:600;font-size:12px;">${p.name || ''}</div>
          ${p.note ? `<div style="margin-top:6px;color:#555;font-size:10px;
                                  line-height:1.4;">${p.note}</div>` : ''}
        </div>`;
      new maplibregl.Popup({ closeButton: true, maxWidth: "280px" })
        .setLngLat(f.geometry.coordinates)
        .setHTML(html)
        .addTo(map);
    };
    map.on("click", ASSETS_LAYER_ID, popupForAsset);
    map.on("mouseenter", ASSETS_LAYER_ID, () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", ASSETS_LAYER_ID, () => {
      map.getCanvas().style.cursor = "";
    });

    map.on("click", SAR_DETECTIONS_LAYER_ID, popupForDetection);
    map.on("click", SAR_SCENES_FILL_LAYER_ID, popupForScene);
    map.on("click", S2_SCENES_FILL_LAYER_ID, popupForS2Scene);
    map.on("click", BUOYS_LAYER_ID, popupForBuoy);
    map.on("click", CONVOY_LINE_LAYER_ID, popupForConvoy);
    map.on("mouseenter", CONVOY_LINE_LAYER_ID, () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", CONVOY_LINE_LAYER_ID, () => {
      map.getCanvas().style.cursor = "";
    });
    map.on("mouseenter", BUOYS_LAYER_ID, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", BUOYS_LAYER_ID, () => { map.getCanvas().style.cursor = ""; });
    map.on("mouseenter", SAR_DETECTIONS_LAYER_ID, () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", SAR_DETECTIONS_LAYER_ID, () => {
      map.getCanvas().style.cursor = "";
    });

    map.on("load", () => setReady(true));
    map.on("error", (e) => {
      // OpenFreeMap occasionally returns 503 during deploys; degrade silently.
      // eslint-disable-next-line no-console
      console.warn("maplibre error", e?.error?.message || e);
    });

    mapRef.current = map;

    return () => {
      // Tear down on unmount (including React 18 strict-mode double-mount).
      for (const m of markersRef.current.values()) m.remove();
      markersRef.current.clear();
      map.remove();
      mapRef.current = null;
      fitDoneRef.current = false;
    };
  }, []);

  // ------------------------------------------------------------------
  // Apply the chosen basemap. setStyle() wipes layers; the 'style.load'
  // listener above re-adds the track source/layers. Markers are DOM
  // elements layered on top of the canvas, so they survive style swaps.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.setStyle(BASEMAPS[basemap].style);
    try {
      window.localStorage.setItem(BASEMAP_STORAGE_KEY, basemap);
    } catch {
      // localStorage may be disabled in some contexts; silently ignore.
    }
  }, [basemap]);

  // ------------------------------------------------------------------
  // When scrubbing back in time, fetch a /timeline snapshot.
  // Cancel in-flight on slider change so the last value wins.
  // ------------------------------------------------------------------
  useEffect(() => {
    if (scrubMinutes === 0) {
      setScrubSnapshot(null);
      setScrubLoading(false);
      return;
    }
    const ctrl = new AbortController();
    const at = new Date(Date.now() - scrubMinutes * 60_000).toISOString();
    setScrubLoading(true);
    fetchTimeline(cfg.apiPath, at, ctrl.signal)
      .then((data) => {
        if (ctrl.signal.aborted) return;
        setScrubSnapshot((data.snapshot || []).map(snapshotToEntity));
        setScrubLoading(false);
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.warn("timeline fetch failed:", err);
        setScrubLoading(false);
      });
    return () => ctrl.abort();
  }, [scrubMinutes, cfg.apiPath]);

  // Source the markers either from live `entities` (now) or the
  // snapshot fetched for the scrubbed-to time.
  const renderEntities = scrubSnapshot ?? entities;

  // ------------------------------------------------------------------
  // Sync markers whenever entities change. Reuse existing marker DOM
  // when the entity is unchanged so we don't churn through 1000s of
  // create/destroys every time AISStream pushes a new observation.
  // ------------------------------------------------------------------
  const entitiesById = useMemo(() => {
    const m = new Map();
    for (const e of renderEntities) m.set(e.id, e);
    return m;
  }, [renderEntities]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    const liveIds = new Set();

    for (const e of renderEntities) {
      if (typeof e.lon !== "number" || typeof e.lat !== "number") continue;
      liveIds.add(e.id);

      const meta = cfg.typeMeta[e.type];
      if (!meta) continue;

      let marker = markersRef.current.get(e.id);
      let el = marker?.getElement();

      if (!marker) {
        el = document.createElement("button");
        el.type = "button";
        el.className = "ss-marker";
        Object.assign(el.style, {
          appearance: "none",
          background: "transparent",
          border: "none",
          padding: 0,
          cursor: "pointer",
          width: "16px",
          height: "16px",
          display: "block",
        });
        el.addEventListener("click", (ev) => {
          ev.stopPropagation();
          onSelect(e.id);
        });
        marker = new maplibregl.Marker({ element: el, anchor: "center" })
          .setLngLat([e.lon, e.lat])
          .addTo(map);
        markersRef.current.set(e.id, marker);
      } else {
        marker.setLngLat([e.lon, e.lat]);
      }

      // Render a small SVG inside each marker — this avoids touching CSS
      // files and lets us tint per-entity.
      const isSelected = e.id === selectedId;
      const r = entityRadius(e.type);
      const stroke = isSelected ? "#ffffff" : "rgba(255,255,255,0.6)";
      const strokeWidth = isSelected ? 2 : 1;
      const cross = e.type === "false_positive";

      // Heading: prefer true_heading (compass), fall back to AIS heading
      // attribute, then COG (course over ground). AISStream sometimes
      // ships any of the three. Heading 511 is "not available" per
      // ITU-R M.1371; we treat that as null.
      const a = e.attrs || {};
      const rawHeading = (
        a.true_heading ?? a.heading ?? a.cog_deg ?? a.cog ?? null
      );
      const headingDeg = (typeof rawHeading === "number"
                          && rawHeading >= 0
                          && rawHeading < 360
                          && rawHeading !== 511)
        ? rawHeading : null;
      // Only show arrows for moving vessels — anchored / moored boats
      // would have stale heading. Speed > 1 kn ≈ "actually under way".
      const speed = a.speed_kn ?? a.sog_kn ?? null;
      const showArrow = (
        headingDeg != null
        && (e.type === "vessel" || e.type === "ais_gap")
        && (speed == null || speed > 1.0)
      );
      // SVG arrow: a chevron 0..-12 along the y axis (north). Outer
      // <g transform="rotate(N)"> rotates it to the heading direction.
      const arrowSvg = showArrow ? `
        <g transform="rotate(${headingDeg})">
          <path d="M 0 -${r + 8} L -3 -${r + 1} L 3 -${r + 1} Z"
                fill="${meta.color}"
                stroke="${stroke}" stroke-width="0.8"
                stroke-linejoin="round" />
        </g>` : "";

      // Pick a vessel-type glyph if the AIS message carries a ship_type
      // and the entity is a cooperative vessel (not a SAR-only dark
      // contact, fire event, etc — those have no AIS to read).
      const shipType = a.ship_type;
      const useGlyph = (e.type === "vessel"
                        || e.type === "ais_gap"
                        || e.type === "loitering_vessel"
                        || e.type === "ais_spoofed"
                        || e.type === "port_skipping")
                       && shipType != null && !cross;
      const glyphSvg = useGlyph
        ? _vesselGlyph(shipType, meta.color, stroke, strokeWidth)
        : null;

      // Anchored vessels and ones with no heading get a non-rotated
      // glyph — rotating to 0° (north) by default would mis-imply
      // a direction the AIS didn't report.
      const rotatable = useGlyph && glyphSvg && headingDeg != null
                        && (e.type === "vessel" || e.type === "ais_gap")
                        && (speed == null || speed > 1.0);

      el.innerHTML = `
        <svg viewBox="-12 -12 24 24" width="20" height="20"
             style="overflow:visible;display:block;">
          ${meta.glow ? `<circle r="14" fill="${meta.color}" opacity="0.18"/>` : ""}
          ${isSelected ? `<circle r="${r + 5}" fill="none"
                                  stroke="${meta.color}" stroke-width="1"
                                  opacity="0.7">
                            <animate attributeName="r"
                                     values="${r + 3};${r + 9};${r + 3}"
                                     dur="2s" repeatCount="indefinite"/>
                          </circle>` : ""}
          ${cross ? `<g>
                       <circle r="${r}" fill="none" stroke="${meta.color}" stroke-width="1"/>
                       <line x1="-4" y1="-4" x2="4" y2="4"
                             stroke="${meta.color}" stroke-width="1.5"/>
                     </g>`
                  : (glyphSvg
                       ? (rotatable
                            ? `<g transform="rotate(${headingDeg})">${glyphSvg}</g>`
                            : `<g>${glyphSvg}</g>`)
                       : `<circle r="${r}" fill="${meta.color}"
                                   stroke="${stroke}" stroke-width="${strokeWidth}"/>`
                    )}
          ${glyphSvg ? "" : arrowSvg}
        </svg>
      `;
      el.title = `${meta.label || e.type}${e.name ? ` — ${e.name}` : ""}` +
                 `${headingDeg != null ? ` · ${Math.round(headingDeg)}°` : ""}` +
                 `${speed != null ? ` · ${Number(speed).toFixed(1)} kn` : ""}`;
      // Bump marker size to 20px to accommodate the arrow without
      // clipping; CSS controls per-marker sizing on the button el.
      el.style.width = "20px";
      el.style.height = "20px";
    }

    // Remove markers that are no longer in the entity set.
    for (const [id, marker] of markersRef.current) {
      if (!liveIds.has(id)) {
        marker.remove();
        markersRef.current.delete(id);
      }
    }

    // First non-empty data → auto-fit. After that, leave the user's pan/zoom
    // alone so refreshes don't snap them out of context. We only fit to
    // entities that fall inside the AOI clamp box — see _insideFitClamp.
    if (!fitDoneRef.current && renderEntities.length > 0) {
      const bounds = new maplibregl.LngLatBounds();
      let any = false;
      for (const e of renderEntities) {
        if (_insideFitClamp(e.lon, e.lat)) {
          bounds.extend([e.lon, e.lat]);
          any = true;
        }
      }
      if (any) {
        map.fitBounds(bounds, { padding: FIT_PADDING, duration: 0, maxZoom: 9 });
        fitDoneRef.current = true;
      }
    }
  }, [renderEntities, entitiesById, selectedId, cfg, ready, onSelect]);

  // ------------------------------------------------------------------
  // Selection change → fetch the track and render it as a polyline.
  // Cancel any in-flight request when the selection changes again.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    const src = map.getSource(TRACK_SOURCE_ID);
    if (!src) return;

    if (!selectedId) {
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }

    const ent = entitiesById.get(selectedId);
    const meta = ent ? cfg.typeMeta[ent.type] : null;
    if (meta?.color) {
      map.setPaintProperty(TRACK_LAYER_ID, "line-color", meta.color);
      map.setPaintProperty(TRACK_HEAD_LAYER_ID, "circle-color", meta.color);
      // Dashed line for ais_gap to mirror the SVG version's convention.
      map.setPaintProperty(
        TRACK_LAYER_ID,
        "line-dasharray",
        ent.type === "ais_gap" ? [2, 2] : [1, 0],
      );
    }

    const ctrl = new AbortController();
    fetchTrack(cfg.apiPath, selectedId, ctrl.signal)
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const points = (data.track || []).filter(
          (p) => typeof p.lon === "number" && typeof p.lat === "number",
        );
        // Pre-parse timestamps once; the render-clip pass below uses .ts
        // for fast filtering on every scrub change.
        for (const p of points) p.ts = Date.parse(p.t);
        setTrackPoints(points);
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.warn("track fetch failed:", err);
        setTrackPoints([]);
      });

    return () => ctrl.abort();
  }, [selectedId, ready, entitiesById, cfg]);

  // ------------------------------------------------------------------
  // Pinned-track render — same shape as the selected-track effect but
  // tied to pinnedId. Clears the pinned source when there's no pin.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(PINNED_TRACK_SOURCE_ID);
    if (!src) return;
    if (!pinnedId || pinnedId === selectedId) {
      // Don't draw the pinned track twice if it equals the selection.
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    const ctrl = new AbortController();
    fetchTrack(cfg.apiPath, pinnedId, ctrl.signal)
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const points = (data.track || []).filter(
          (p) => typeof p.lon === "number" && typeof p.lat === "number",
        );
        if (points.length < 2) {
          src.setData({ type: "FeatureCollection", features: [] });
          return;
        }
        const features = [{
          type: "Feature",
          geometry: {
            type: "LineString",
            coordinates: points.map((p) => [p.lon, p.lat]),
          },
          properties: {},
        }];
        for (const p of points) {
          features.push({
            type: "Feature",
            geometry: { type: "Point", coordinates: [p.lon, p.lat] },
            properties: {},
          });
        }
        src.setData({ type: "FeatureCollection", features });
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.warn("pinned track fetch failed:", err);
      });
    return () => ctrl.abort();
  }, [pinnedId, selectedId, ready, cfg]);

  // ------------------------------------------------------------------
  // Auto-zoom on select: when the operator picks an entity from the
  // queue OR clicks one on the map, smoothly fly to it at vessel-
  // visible zoom (z=13 ≈ 1 px = ~10 m at this latitude — matches the
  // S2 GSD, so a vessel is about 1-2 pixels). Only re-zooms if the
  // current zoom is well below z=13, so an operator who has already
  // zoomed in further doesn't get yanked back out.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !selectedId) return;
    const ent = entitiesById.get(selectedId);
    if (!ent || typeof ent.lon !== "number" || typeof ent.lat !== "number") return;
    if (map.getZoom() >= 12.5) {
      // Already close enough — just pan smoothly.
      map.easeTo({ center: [ent.lon, ent.lat], duration: 600 });
    } else {
      map.flyTo({
        center: [ent.lon, ent.lat],
        zoom: 13,
        duration: 900,
        essential: true,   // respect prefers-reduced-motion but still fly
      });
    }
  }, [selectedId, ready, entitiesById]);

  // ------------------------------------------------------------------
  // Candidate-hull halos: when an anomaly is selected, ring the 5-km
  // neighborhood of cooperative AIS vessels on the map. Matches the
  // "Nearby candidate hulls" side-panel rows so the operator sees the
  // investigation spatially. Effect resets when:
  //   - selectedId changes (new anomaly → new candidates)
  //   - renderEntities changes (live AIS observations may have moved
  //     a vessel into or out of the radius)
  //   - selection is NOT an anomaly (e.g. user clicked a cooperative
  //     vessel) → halos cleared.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(CANDIDATES_SOURCE_ID);
    if (!src) return;

    if (!selectedId) {
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    const sel = entitiesById.get(selectedId);
    if (!sel || !SUSPECT_TYPES_MAP.has(sel.type)) {
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    if (typeof sel.lon !== "number" || typeof sel.lat !== "number") {
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }

    const origin = { lon: sel.lon, lat: sel.lat };
    const rows = [];
    for (const e of renderEntities) {
      if (e.id === sel.id) continue;
      if (e.type !== "vessel" && e.type !== "ais_gap") continue;
      if (typeof e.lon !== "number" || typeof e.lat !== "number") continue;
      const d = _haversineKm(origin, { lon: e.lon, lat: e.lat });
      if (d > CANDIDATE_RADIUS_KM) continue;
      rows.push({ ent: e, d });
    }
    rows.sort((a, b) => a.d - b.d);
    const top = rows.slice(0, CANDIDATE_MAX);

    src.setData({
      type: "FeatureCollection",
      features: top.map(({ ent, d }) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [ent.lon, ent.lat] },
        properties: {
          id: ent.id,
          mmsi: ent.mmsi || null,
          name: ent.name || null,
          distance_km: d,
        },
      })),
    });
  }, [selectedId, ready, entitiesById, renderEntities]);

  // ------------------------------------------------------------------
  // Anomaly state-transition watcher — drives both the audio ping for
  // new dark vessels and the corner toast stream for any new anomaly
  // classification. First entities-prop arrival silently seeds
  // lastTypeRef so we don't get a wall of toasts at page load.
  // ------------------------------------------------------------------
  useEffect(() => {
    const typeMap = lastTypeRef.current;
    if (!initializedRef.current) {
      // Seed silently — every entity gets its current type recorded with
      // no toast/ping. Subsequent ticks compare against this baseline.
      for (const e of renderEntities) typeMap.set(e.id, e.type);
      if (renderEntities.length > 0) initializedRef.current = true;
      return;
    }
    const newToasts = [];
    let anyNewDarkVessel = false;
    // Pre-index positions of dark/spoofed vessels — we look up each
    // gap candidate against this so "gap appeared near a known dark
    // vessel" gets through the suppression filter.
    const darkPositions = [];
    for (const o of renderEntities) {
      if (o.type === "dark_vessel" || o.type === "ais_spoofed") {
        if (typeof o.lon === "number" && typeof o.lat === "number") {
          darkPositions.push([o.lon, o.lat]);
        }
      }
    }
    for (const e of renderEntities) {
      const prev = typeMap.get(e.id);
      if (prev === e.type) continue;
      typeMap.set(e.id, e.type);
      if (!ANOMALY_ALERT_TYPES.has(e.type)) continue;
      // ais_gap is the spammy one — the gap sweeper creates these
      // continuously as the AIS feed naturally drops connections.
      // Two cases survive the suppression because they're actually
      // interesting:
      //   (a) the gap is within 5 km of a dark/spoofed vessel — a
      //       cooperative vessel going silent next to a non-coop
      //       target is a real correlation signal
      //   (b) the gap is in empty water — no other AIS-cooperative
      //       vessel within 10 km. A gap in a busy lane is noise;
      //       a gap with no neighbors is a vessel deliberately
      //       slipping away from witnesses.
      if (e.type === "ais_gap") {
        let interesting = false;
        if (typeof e.lon === "number" && typeof e.lat === "number") {
          // Case (a) — near dark/spoofed
          for (const [olon, olat] of darkPositions) {
            if (_haversineKm({ lon: e.lon, lat: e.lat },
                             { lon: olon, lat: olat }) <= 5.0) {
              interesting = true;
              break;
            }
          }
          if (!interesting) {
            // Case (b) — empty water. Count cooperative neighbors.
            let neighbors = 0;
            for (const o of renderEntities) {
              if (o.id === e.id) continue;
              if (o.type !== "vessel" && o.type !== "ais_gap") continue;
              if (typeof o.lon !== "number" || typeof o.lat !== "number") continue;
              if (_haversineKm({ lon: e.lon, lat: e.lat },
                               { lon: o.lon, lat: o.lat }) <= 10.0) {
                neighbors += 1;
                if (neighbors >= 1) break;   // anything close-by → skip
              }
            }
            if (neighbors === 0) interesting = true;
          }
        }
        if (!interesting) continue;
      }
      const meta = cfg.typeMeta[e.type];
      if (!meta) continue;
      newToasts.push({
        id: `${e.id}_${e.type}_${Date.now()}`,
        entity_id: e.id,
        name: e.name,
        mmsi: e.mmsi,
        from: prev,
        to: e.type,
        meta,
        ts: Date.now(),
      });
      if (e.type === "dark_vessel") anyNewDarkVessel = true;
    }
    if (newToasts.length > 0) {
      setToasts((cur) => [...newToasts, ...cur].slice(0, TOAST_MAX_VISIBLE));
    }
    if (anyNewDarkVessel && soundEnabled) {
      _playDarkVesselPing();
    }
  }, [renderEntities, cfg, soundEnabled]);

  // Auto-dismiss toasts after TOAST_TTL_MS. Single interval scans the
  // list and culls anything past its TTL.
  useEffect(() => {
    if (toasts.length === 0) return;
    const interval = setInterval(() => {
      const now = Date.now();
      setToasts((cur) => cur.filter((t) => now - t.ts < TOAST_TTL_MS));
    }, 500);
    return () => clearInterval(interval);
  }, [toasts.length]);

  // AOI draw layer — re-render the in-progress polyline (while
  // 'drawing') or the closed polygon (while 'fixed') on every state
  // change. Vertex points are always emitted as additional Point
  // features so the operator sees where they've clicked.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(AOI_DRAW_SOURCE_ID);
    if (!src) return;
    if (aoiMode === "idle" || aoiPoints.length === 0) {
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    const features = aoiPoints.map((p) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: p },
      properties: {},
    }));
    if (aoiMode === "drawing" && aoiPoints.length >= 2) {
      features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: aoiPoints },
        properties: {},
      });
    }
    if (aoiMode === "fixed" && aoiPoints.length >= 3) {
      // Close the polygon ring.
      features.push({
        type: "Feature",
        geometry: {
          type: "Polygon",
          coordinates: [[...aoiPoints, aoiPoints[0]]],
        },
        properties: {},
      });
    }
    src.setData({ type: "FeatureCollection", features });
  }, [aoiPoints, aoiMode, ready]);

  // Escape clears AOI drawing or the fixed AOI itself.
  useEffect(() => {
    if (aoiMode === "idle") return;
    const handler = (e) => {
      if (e.key === "Escape") {
        setAoiMode("idle");
        setAoiPoints([]);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [aoiMode]);

  // Map cursor changes to crosshair while drawing.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const canvas = map.getCanvas();
    canvas.style.cursor = aoiMode === "drawing" ? "crosshair" : "";
    return () => { canvas.style.cursor = ""; };
  }, [aoiMode, ready]);

  // Count entities inside the fixed AOI — used by the floating
  // result chip below.
  const aoiCount = useMemo(() => {
    if (aoiMode !== "fixed" || aoiPoints.length < 3) return 0;
    return renderEntities.filter(
      (e) => typeof e.lon === "number" && typeof e.lat === "number" &&
             _pointInPolygon(e.lon, e.lat, aoiPoints),
    ).length;
  }, [aoiMode, aoiPoints, renderEntities]);

  // ------------------------------------------------------------------
  // Activity heatmap — feed current vessel positions into the heatmap
  // layer and toggle its visibility. Re-runs whenever entities change
  // OR the heatmap is toggled, so live AIS updates are reflected.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(HEATMAP_SOURCE_ID);
    if (!src) return;
    // Only feed COOPERATIVE positions (vessels + ais_gap) into the
    // density layer. Including dark/spoofed/loitering would distort
    // the "where is the normal traffic" story — those are the very
    // things the operator wants context FOR.
    const pts = renderEntities.filter(
      (e) => (e.type === "vessel" || e.type === "ais_gap")
             && typeof e.lon === "number" && typeof e.lat === "number",
    );
    src.setData({
      type: "FeatureCollection",
      features: pts.map((e) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [e.lon, e.lat] },
        properties: {},
      })),
    });
    if (map.getLayer(HEATMAP_LAYER_ID)) {
      map.setLayoutProperty(
        HEATMAP_LAYER_ID, "visibility",
        heatmapOn ? "visible" : "none",
      );
    }
  }, [renderEntities, heatmapOn, ready]);

  // ------------------------------------------------------------------
  // Active dispatch rings — recompute features whenever the dispatch
  // list OR the entity list changes (we need the entity's current
  // position to draw the ring at the right place).
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(ACTIVE_DISPATCH_SOURCE_ID);
    if (!src) return;
    const features = [];
    for (const d of activeDispatches) {
      const ent = entitiesById.get(d.entity_id);
      if (!ent) continue;
      if (typeof ent.lon !== "number" || typeof ent.lat !== "number") continue;
      const ageMin = Math.max(0, Math.round(
        (Date.now() - d.dispatched_at_ms) / 60_000));
      const ageStr = ageMin < 60 ? `${ageMin}m` : `${(ageMin / 60).toFixed(1)}h`;
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [ent.lon, ent.lat] },
        properties: {
          entity_id: d.entity_id,
          label: `DISPATCHED · ${ageStr} ago`,
        },
      });
    }
    src.setData({ type: "FeatureCollection", features });
  }, [activeDispatches, entitiesById, ready]);

  // ------------------------------------------------------------------
  // Onshore assets — fetched once on toggle-on. The catalog is static
  // (USCG stations, Navy facilities don't move) so we keep the data
  // in the source forever and just flip visibility on toggle.
  // ------------------------------------------------------------------
  const assetsLoadedRef = useRef(false);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (map.getLayer(ASSETS_LAYER_ID)) {
      const v = assetsOn ? "visible" : "none";
      map.setLayoutProperty(ASSETS_LAYER_ID, "visibility", v);
      map.setLayoutProperty(ASSETS_LABEL_LAYER_ID, "visibility", v);
    }
    if (!assetsOn || assetsLoadedRef.current) return;
    const ctrl = new AbortController();
    fetch(`${API_BASE}/maritime/onshore_assets`, { signal: ctrl.signal })
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data) return;
        const src = map.getSource(ASSETS_SOURCE_ID);
        if (!src) return;
        src.setData(data);
        assetsLoadedRef.current = true;
      })
      .catch(() => {});
    return () => ctrl.abort();
  }, [assetsOn, ready]);

  // ------------------------------------------------------------------
  // Operator-workload heatmap — periodically pulls the recent-dispatch
  // positions and feeds them into the WORKLOAD layer. Cheap: dispatches
  // are rare (a few per shift) so we poll every 90 s instead of every
  // entities-tick.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (map.getLayer(WORKLOAD_LAYER_ID)) {
      map.setLayoutProperty(
        WORKLOAD_LAYER_ID, "visibility",
        workloadOn ? "visible" : "none",
      );
    }
    if (!workloadOn) return;
    let cancelled = false;
    const pull = () => {
      const ctrl = new AbortController();
      fetch(`${API_BASE}/maritime/dispatches/recent?hours=24`,
            { signal: ctrl.signal })
        .then((r) => r.ok ? r.json() : null)
        .then((data) => {
          if (cancelled || !data) return;
          const src = map.getSource(WORKLOAD_SOURCE_ID);
          if (!src) return;
          src.setData({
            type: "FeatureCollection",
            features: (data.dispatches || []).map((d) => ({
              type: "Feature",
              geometry: { type: "Point", coordinates: [d.lon, d.lat] },
              properties: { entity_id: d.entity_id, t: d.dispatched_at },
            })),
          });
        })
        .catch(() => { /* network blips are fine — try again on interval */ });
    };
    pull();
    const id = window.setInterval(pull, WORKLOAD_REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [workloadOn, ready]);

  // ------------------------------------------------------------------
  // Anomaly trails — fetch the last-hour track for every anomaly
  // entity (capped at ANOMALY_TRAIL_MAX) and render as a fading
  // polyline tinted to the entity's typeMeta color. Caches per-entity
  // tracks by (id, last_seen) so the fetch doesn't re-fire on every
  // tick — only when an entity is new or has reported a new position.
  // ------------------------------------------------------------------
  const trailCacheRef = useRef(new Map()); // id → { last_seen, points }
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(ANOMALY_TRAILS_SOURCE_ID);
    if (!src) return;

    // Pick the anomalies we'd actually want trails for. Sort by
    // priority desc so high-priority entities win the cap.
    const candidates = renderEntities
      .filter((e) => ANOMALY_TRAIL_TYPES.has(e.type))
      .sort((a, b) => (b.priority || 0) - (a.priority || 0))
      .slice(0, ANOMALY_TRAIL_MAX);

    let cancelled = false;
    const cache = trailCacheRef.current;

    const cutoffMs = Date.now() - trailWindowMs;
    const renderFromCache = () => {
      if (cancelled) return;
      const features = [];
      for (const e of candidates) {
        const c = cache.get(e.id);
        if (!c || !c.points || c.points.length < 2) continue;
        // Filter track points to the active window. Track point .ts
        // is already pre-parsed by fetchTrack-time post-processing
        // elsewhere; if missing, fall back to parsing here.
        const filtered = c.points.filter((p) => {
          const ts = p.ts ?? (p.t ? Date.parse(p.t) : NaN);
          return !Number.isNaN(ts) && ts >= cutoffMs;
        });
        if (filtered.length < 2) continue;
        const meta = cfg.typeMeta[e.type];
        features.push({
          type: "Feature",
          geometry: {
            type: "LineString",
            coordinates: filtered.map((p) => [p.lon, p.lat]),
          },
          properties: {
            entity_id: e.id,
            color: (meta && meta.color) || "#ff5c5c",
            type: e.type,
          },
        });
      }
      src.setData({ type: "FeatureCollection", features });
    };

    // Render immediately from whatever's cached; fetch missing tracks.
    renderFromCache();

    const ctrls = [];
    for (const e of candidates) {
      const cached = cache.get(e.id);
      // Re-fetch when the entity is new or has a fresher last_seen.
      if (cached && cached.last_seen === e.last_seen) continue;
      const ctrl = new AbortController();
      ctrls.push(ctrl);
      fetchTrack(cfg.apiPath, e.id, ctrl.signal)
        .then((data) => {
          if (cancelled) return;
          const pts = (data.track || []).filter(
            (p) => typeof p.lon === "number" && typeof p.lat === "number",
          );
          // Pre-parse timestamps so the renderFromCache window filter
          // doesn't re-Date.parse on every trail-window toggle tick.
          for (const p of pts) p.ts = Date.parse(p.t);
          cache.set(e.id, { last_seen: e.last_seen, points: pts });
          renderFromCache();
        })
        .catch((err) => {
          if (err.name === "AbortError") return;
          // eslint-disable-next-line no-console
          console.warn(`anomaly trail fetch failed for ${e.id}:`, err);
        });
    }

    // Evict cache for entities that aren't candidates anymore so the
    // map doesn't drift to a stale cardinality.
    const liveIds = new Set(candidates.map((e) => e.id));
    for (const id of [...cache.keys()]) {
      if (!liveIds.has(id)) cache.delete(id);
    }

    return () => {
      cancelled = true;
      for (const c of ctrls) c.abort();
    };
  }, [renderEntities, cfg, ready, trailWindowMs]);

  // ------------------------------------------------------------------
  // Convoy lines: group entities by attrs.convoy_id (backend-tagged by
  // detect_convoys), then emit one LineString per convoy connecting
  // its members in MMSI order. Each member also gets a Point feature
  // so the cyan ring is visible regardless of zoom.
  //
  // Re-runs on every entities change because AIS observations move
  // members and the engine can re-cluster on the next sweep — a
  // member that drifts out of formation drops its convoy_id and the
  // line shortens automatically.
  // ------------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(CONVOY_SOURCE_ID);
    if (!src) return;

    const groups = new Map();
    for (const e of renderEntities) {
      const cid = e.attrs && e.attrs.convoy_id;
      if (!cid) continue;
      if (typeof e.lon !== "number" || typeof e.lat !== "number") continue;
      if (!groups.has(cid)) groups.set(cid, []);
      groups.get(cid).push(e);
    }

    const features = [];
    for (const [cid, members] of groups) {
      if (members.length < 2) continue;
      // Sort by MMSI for a stable line order across renders (so the
      // dashed pattern doesn't shimmer when an AIS update arrives).
      members.sort((a, b) => String(a.mmsi || "").localeCompare(String(b.mmsi || "")));
      features.push({
        type: "Feature",
        geometry: {
          type: "LineString",
          coordinates: members.map((m) => [m.lon, m.lat]),
        },
        properties: {
          convoy_id: cid,
          n_members: members.length,
          // Stringify member metadata onto the feature so the line's
          // click handler (registered once at map init time) can render
          // a member list without a stale closure over renderEntities.
          // MapLibre passes properties through as JSON-flat values, so
          // a JSON string is the lingua franca.
          members_json: JSON.stringify(members.map((m) => ({
            id: m.id,
            mmsi: m.mmsi || null,
            name: m.name || null,
            speed_kn: m.attrs && m.attrs.speed_kn != null
              ? m.attrs.speed_kn : null,
            heading: m.attrs && m.attrs.heading != null
              ? m.attrs.heading : null,
          }))),
        },
      });
      for (const m of members) {
        features.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [m.lon, m.lat] },
          properties: {
            convoy_id: cid,
            entity_id: m.id,
            mmsi: m.mmsi || null,
          },
        });
      }
    }
    src.setData({ type: "FeatureCollection", features });
  }, [renderEntities, ready]);

  // ------------------------------------------------------------------
  // SAR overlay: fetch when toggled on (or never if off). Cheap to
  // refetch — scenes are <50 features, detections grow ~200-1000/scene.
  // ------------------------------------------------------------------
  useEffect(() => {
    if (!showSar) {
      setSarData(null);
      return;
    }
    const ctrl = new AbortController();
    fetchSarOverlay(ctrl.signal)
      .then((data) => {
        if (ctrl.signal.aborted) return;
        setSarData(data);
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.warn("sar overlay fetch failed:", err);
        setSarData({ scenes: { type: "FeatureCollection", features: [] },
                     detections: { type: "FeatureCollection", features: [] } });
      });
    try {
      window.localStorage.setItem(SAR_STORAGE_KEY, "1");
    } catch {
      // ignore — same rationale as basemap persistence
    }
    return () => ctrl.abort();
  }, [showSar]);

  // Persist the off state too so a deliberate flip-off survives reload.
  useEffect(() => {
    if (showSar) return;
    try {
      window.localStorage.setItem(SAR_STORAGE_KEY, "0");
    } catch {
      // ignore
    }
  }, [showSar]);

  // Push fetched SAR data into the layer sources. Empty FeatureCollections
  // when toggled off so the layers go invisible without re-fetching.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const scenesSrc = map.getSource(SAR_SCENES_SOURCE_ID);
    const detSrc = map.getSource(SAR_DETECTIONS_SOURCE_ID);
    if (!scenesSrc || !detSrc) return;
    const empty = { type: "FeatureCollection", features: [] };
    if (showSar && sarData) {
      scenesSrc.setData(sarData.scenes || empty);
      detSrc.setData(sarData.detections || empty);
    } else {
      scenesSrc.setData(empty);
      detSrc.setData(empty);
    }
  }, [sarData, showSar, ready]);

  // S2 fetch + persist toggle. Same shape as SAR; one less layer
  // because we have no per-detection point data yet.
  useEffect(() => {
    if (!showS2) { setS2Data(null); return; }
    const ctrl = new AbortController();
    fetchS2Overlay(ctrl.signal)
      .then((data) => { if (!ctrl.signal.aborted) setS2Data(data); })
      .catch((err) => {
        if (err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.warn("s2 overlay fetch failed:", err);
        setS2Data({ type: "FeatureCollection", features: [] });
      });
    try { window.localStorage.setItem(S2_STORAGE_KEY, "1"); } catch { /* ignore */ }
    return () => ctrl.abort();
  }, [showS2]);
  useEffect(() => {
    if (showS2) return;
    try { window.localStorage.setItem(S2_STORAGE_KEY, "0"); } catch { /* ignore */ }
  }, [showS2]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(S2_SCENES_SOURCE_ID);
    if (!src) return;
    const empty = { type: "FeatureCollection", features: [] };
    src.setData(showS2 && s2Data ? s2Data : empty);
  }, [s2Data, showS2, ready]);

  // Buoys: fetch on toggle + auto-refresh every BUOYS_REFRESH_MS while
  // the layer is visible (NDBC updates ~30 min/station).
  useEffect(() => {
    if (!showBuoys) { setBuoysData(null); return; }
    let cancelled = false;
    const ctrl = new AbortController();
    const pull = () => {
      fetchBuoys(ctrl.signal)
        .then((data) => { if (!cancelled) setBuoysData(data); })
        .catch((err) => {
          if (err.name === "AbortError") return;
          // eslint-disable-next-line no-console
          console.warn("buoys fetch failed:", err);
        });
    };
    pull();
    const interval = window.setInterval(pull, BUOYS_REFRESH_MS);
    try { window.localStorage.setItem(BUOYS_STORAGE_KEY, "1"); } catch { /* ignore */ }
    return () => { cancelled = true; ctrl.abort(); window.clearInterval(interval); };
  }, [showBuoys]);
  useEffect(() => {
    if (showBuoys) return;
    try { window.localStorage.setItem(BUOYS_STORAGE_KEY, "0"); } catch { /* ignore */ }
  }, [showBuoys]);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(BUOYS_SOURCE_ID);
    if (!src) return;
    const empty = { type: "FeatureCollection", features: [] };
    src.setData(showBuoys && buoysData ? buoysData : empty);
  }, [buoysData, showBuoys, ready]);

  // GOES overlay: mode-driven visibility + tile-URL swap. setTiles()
  // both updates the source and invalidates MapLibre's tile cache so
  // a fresh fetch happens for the new mode (or the next 10-min frame).
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (!map.getLayer(GOES_LAYER_ID)) return;
    try { window.localStorage.setItem(GOES_STORAGE_KEY, goesMode); }
    catch { /* ignore */ }
    if (goesMode === "off") {
      map.setLayoutProperty(GOES_LAYER_ID, "visibility", "none");
      return;
    }
    const url = GOES_TILE_URLS[goesMode] || GOES_TILE_URLS.geocolor;
    const src = map.getSource(GOES_SOURCE_ID);
    if (src && src.setTiles) src.setTiles([url]);
    map.setLayoutProperty(GOES_LAYER_ID, "visibility", "visible");
    // Periodic refresh — re-set tiles spec so MapLibre re-pulls fresh
    // imagery as GIBS publishes new 10-min frames.
    const interval = window.setInterval(() => {
      const s = map.getSource(GOES_SOURCE_ID);
      if (s && s.setTiles) s.setTiles([url]);
    }, GOES_REFRESH_MS);
    return () => window.clearInterval(interval);
  }, [goesMode, ready]);

  // VIIRS overlay — mirror of the GOES effect; different cadence + URLs.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (!map.getLayer(VIIRS_LAYER_ID)) return;
    try { window.localStorage.setItem(VIIRS_STORAGE_KEY, viirsMode); }
    catch { /* ignore */ }
    if (viirsMode === "off") {
      map.setLayoutProperty(VIIRS_LAYER_ID, "visibility", "none");
      return;
    }
    const url = VIIRS_TILE_URLS[viirsMode] || VIIRS_TILE_URLS.dnb;
    const src = map.getSource(VIIRS_SOURCE_ID);
    if (src && src.setTiles) src.setTiles([url]);
    map.setLayoutProperty(VIIRS_LAYER_ID, "visibility", "visible");
    const interval = window.setInterval(() => {
      const s = map.getSource(VIIRS_SOURCE_ID);
      if (s && s.setTiles) s.setTiles([url]);
    }, VIIRS_REFRESH_MS);
    return () => window.clearInterval(interval);
  }, [viirsMode, ready]);

  // Render the (cached) track points into the GeoJSON source, clipped at
  // the scrubbed-to time. Cheap effect: just a filter+map over an array.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(TRACK_SOURCE_ID);
    if (!src) return;

    const cutoff = scrubMinutes > 0 ? Date.now() - scrubMinutes * 60_000 : Infinity;
    const visible = trackPoints.filter((p) => p.ts <= cutoff);
    const coords = visible.map((p) => [p.lon, p.lat]);
    const features = [];
    if (coords.length >= 2) {
      features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: coords },
        properties: {},
      });
    }
    for (const c of coords) {
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: c },
        properties: {},
      });
    }
    src.setData({ type: "FeatureCollection", features });
  }, [trackPoints, scrubMinutes, ready]);

  return (
    <div
      style={{
        position: "relative",
        flex: 1,
        background: "#040810",
        overflow: "hidden",
        minHeight: 0,
      }}
    >
      <div
        ref={containerRef}
        style={{ position: "absolute", inset: 0 }}
      />
      {/* Basemap picker. Top-left so it doesn't collide with the
          NavigationControl (top-right) or the ScaleControl (bottom-left). */}
      <div
        style={{
          position: "absolute",
          top: 8,
          left: 8,
          display: "flex",
          gap: 1,
          background: "rgba(4,8,16,0.78)",
          border: "1px solid rgba(255,255,255,0.18)",
          borderRadius: 4,
          overflow: "hidden",
          fontFamily:
            "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: 11,
          letterSpacing: "0.06em",
          zIndex: 1,
        }}
      >
        {Object.entries(BASEMAPS).map(([key, def]) => {
          const active = key === basemap;
          return (
            <button
              key={key}
              type="button"
              onClick={() => setBasemap(key)}
              style={{
                appearance: "none",
                border: "none",
                cursor: "pointer",
                padding: "6px 10px",
                background: active ? "rgba(95,208,147,0.18)" : "transparent",
                color: active ? "#cdf2dd" : "rgba(255,255,255,0.7)",
                textTransform: "uppercase",
              }}
            >
              {def.label}
            </button>
          );
        })}
        <button
          type="button"
          title="Toggle Sentinel-1 SAR scenes + detections"
          onClick={() => setShowSar((v) => !v)}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background: showSar ? "rgba(240,168,48,0.22)" : "transparent",
            color: showSar ? "#ffd897" : "rgba(255,255,255,0.7)",
            textTransform: "uppercase",
          }}
        >
          SAR
        </button>
        <button
          type="button"
          title="Toggle Sentinel-2 optical scene footprints (≤40% cloud)"
          onClick={() => setShowS2((v) => !v)}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background: showS2 ? "rgba(95,190,208,0.22)" : "transparent",
            color: showS2 ? "#bfe6ee" : "rgba(255,255,255,0.7)",
            textTransform: "uppercase",
          }}
        >
          S2
        </button>
        <button
          type="button"
          title="Toggle live NDBC buoys (NOAA, ~30 min cadence)"
          onClick={() => setShowBuoys((v) => !v)}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background: showBuoys ? "rgba(95,168,208,0.22)" : "transparent",
            color: showBuoys ? "#bfe1ee" : "rgba(255,255,255,0.7)",
            textTransform: "uppercase",
          }}
        >
          BUOY
        </button>
        <button
          type="button"
          title={
            "Cycle GOES-East real-time imagery (NASA GIBS, ~10 min):  " +
            "off → GeoColor (true color) → FireTemp (thermal anomaly)"
          }
          onClick={() => {
            const i = GOES_CYCLE.indexOf(goesMode);
            setGoesMode(GOES_CYCLE[(i + 1) % GOES_CYCLE.length]);
          }}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background:
              goesMode === "off" ? "transparent" :
              goesMode === "firetemp" ? "rgba(255,90,40,0.30)" :
              "rgba(180,160,80,0.22)",
            color:
              goesMode === "off" ? "rgba(255,255,255,0.7)" :
              goesMode === "firetemp" ? "#ffd0b8" :
              "#e8d8a8",
            textTransform: "uppercase",
            minWidth: 56,
          }}
        >
          {goesMode === "off" ? "GOES"
           : goesMode === "firetemp" ? "FIRE"
           : "GEO"}
        </button>
        <button
          type="button"
          title={
            "Toggle VIIRS Day/Night Band (NASA GIBS): lit ships at night."
          }
          onClick={() => {
            const i = VIIRS_CYCLE.indexOf(viirsMode);
            setViirsMode(VIIRS_CYCLE[(i + 1) % VIIRS_CYCLE.length]);
          }}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background:
              viirsMode === "off" ? "transparent" :
              "rgba(160,120,200,0.28)",
            color:
              viirsMode === "off" ? "rgba(255,255,255,0.7)" :
              "#dcc8ee",
            textTransform: "uppercase",
            minWidth: 56,
          }}
        >
          {viirsMode === "off" ? "VIIRS" : "DNB"}
        </button>
        <button
          type="button"
          title="Toggle activity heatmap — shows where cooperative traffic is"
          onClick={() => persistHeatmap(!heatmapOn)}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background: heatmapOn ? "rgba(255,150,80,0.28)" : "transparent",
            color: heatmapOn ? "#ffd2a8" : "rgba(255,255,255,0.7)",
            textTransform: "uppercase",
            minWidth: 56,
            fontSize: 10,
            letterSpacing: "0.08em",
          }}
        >
          ▓ heat
        </button>
        <button
          type="button"
          title="Toggle operator-workload heatmap — recent dispatch positions"
          onClick={() => persistWorkload(!workloadOn)}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background: workloadOn ? "rgba(200,90,210,0.28)" : "transparent",
            color: workloadOn ? "#f0c8ff" : "rgba(255,255,255,0.7)",
            textTransform: "uppercase",
            minWidth: 64,
            fontSize: 10,
            letterSpacing: "0.08em",
          }}
        >
          ▓ ops
        </button>
        <button
          type="button"
          title="Toggle onshore-asset overlay — USCG stations + Navy facilities"
          onClick={() => persistAssets(!assetsOn)}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background: assetsOn ? "rgba(95,208,147,0.22)" : "transparent",
            color: assetsOn ? "#cdf2dd" : "rgba(255,255,255,0.7)",
            textTransform: "uppercase",
            minWidth: 64,
            fontSize: 10,
            letterSpacing: "0.08em",
          }}
        >
          ⚓ assets
        </button>

        {/* Anomaly trail window — 1h / 6h / 24h. Filters the per-
            anomaly polylines client-side from cached track points
            so swapping windows is instant (no extra backend hit). */}
        {["1h", "6h", "24h"].map((w, i) => (
          <button
            key={w}
            type="button"
            title={`Show anomaly trails for last ${w}`}
            onClick={() => persistTrailWindow(w)}
            style={{
              appearance: "none",
              border: "none",
              borderLeft: "1px solid rgba(255,255,255,0.18)",
              cursor: "pointer",
              padding: "6px 8px",
              fontSize: 10,
              fontFamily: "inherit",
              letterSpacing: "0.08em",
              background: trailWindow === w
                ? "rgba(95,208,147,0.22)" : "transparent",
              color: trailWindow === w
                ? "#cdf2dd" : "rgba(255,255,255,0.55)",
              textTransform: "uppercase",
              minWidth: 36,
            }}
          >
            {/* Prefix the first chip with a label so the row reads
                 as a single grouped control. */}
            {i === 0 ? `↳ ${w}` : w}
          </button>
        ))}
        <button
          type="button"
          title={
            aoiMode === "idle"
              ? "Draw a custom area of interest — click corners, double-click to finish"
              : aoiMode === "drawing"
                ? `Click corners on the map (${aoiPoints.length} so far) · double-click to finish · Esc to cancel`
                : "Clear the drawn AOI"
          }
          onClick={() => {
            if (aoiMode === "idle") {
              setAoiPoints([]);
              setAoiMode("drawing");
            } else {
              setAoiMode("idle");
              setAoiPoints([]);
            }
          }}
          style={{
            appearance: "none",
            border: "none",
            borderLeft: "1px solid rgba(255,255,255,0.18)",
            cursor: "pointer",
            padding: "6px 10px",
            background:
              aoiMode === "idle" ? "transparent" : "rgba(240,201,48,0.28)",
            color:
              aoiMode === "idle" ? "rgba(255,255,255,0.7)" : "#ffe7a8",
            textTransform: "uppercase",
            minWidth: 64,
          }}
        >
          {aoiMode === "idle" ? "+ AOI"
           : aoiMode === "drawing" ? `drawing (${aoiPoints.length})`
           : "✕ AOI"}
        </button>
      </div>

      {/* AOI result chip — appears once the polygon is closed, shows
          how many entities fall inside. Sits LEFT side (right-side
          is reserved for the toast stack) and BELOW the basemap toggle
          row. */}
      {aoiMode === "fixed" && aoiPoints.length >= 3 && (
        <div style={{
          position: "absolute",
          top: 52,
          left: 12,
          background: "rgba(4,8,16,0.86)",
          border: "1px solid #f0c930",
          borderRadius: 4,
          padding: "8px 12px",
          fontFamily: "'IBM Plex Mono', monospace",
          fontSize: 11,
          letterSpacing: "0.06em",
          color: "#ffe7a8",
          zIndex: 2,
          display: "flex", flexDirection: "column", gap: 4,
          minWidth: 220,
        }}>
          <div style={{ fontSize: 9, letterSpacing: "0.16em", opacity: 0.7 }}>
            CUSTOM AOI · {aoiPoints.length} VERTICES
          </div>
          <div style={{ fontSize: 14, fontWeight: 600,
                        fontVariantNumeric: "tabular-nums" }}>
            {aoiCount} entit{aoiCount === 1 ? "y" : "ies"} inside
          </div>
          <div style={{ display: "flex", gap: 4, marginTop: 4, flexWrap: "wrap" }}>
            <button
              type="button"
              disabled={savedAois.length >= SAVED_AOI_MAX}
              onClick={() => {
                const name = window.prompt(
                  `Save AOI as (max ${SAVED_AOI_MAX} presets):`,
                  `AOI ${savedAois.length + 1}`);
                if (!name) return;
                const next = [...savedAois, {
                  id: `aoi_${Date.now()}`,
                  name: name.slice(0, 32),
                  points: aoiPoints.slice(),
                  created_at: new Date().toISOString(),
                }];
                persistSavedAois(next);
              }}
              title={savedAois.length >= SAVED_AOI_MAX
                ? `Limit reached — delete a preset to save another`
                : `Save this polygon as a preset`}
              style={{
                appearance: "none",
                border: "1px solid #f0c930",
                background: "rgba(240,201,48,0.18)",
                color: "#ffe7a8",
                borderRadius: 3,
                padding: "3px 8px",
                fontFamily: "inherit",
                fontSize: 9,
                cursor: savedAois.length >= SAVED_AOI_MAX ? "default" : "pointer",
                letterSpacing: "0.12em",
                opacity: savedAois.length >= SAVED_AOI_MAX ? 0.5 : 1,
              }}
            >
              💾 SAVE
            </button>
            <button
              type="button"
              onClick={() => { setAoiMode("idle"); setAoiPoints([]); }}
              style={{
                appearance: "none",
                border: "1px solid rgba(255,255,255,0.2)",
                background: "transparent",
                color: "rgba(255,255,255,0.7)",
                borderRadius: 3,
                padding: "3px 8px",
                fontFamily: "inherit",
                fontSize: 9,
                cursor: "pointer",
                letterSpacing: "0.12em",
              }}
            >
              CLEAR
            </button>
          </div>
        </div>
      )}

      {/* Saved-AOIs picker — small floating menu below the AOI toggle.
          Always present when there's >= 1 saved preset, regardless of
          current draw state, so the operator can recall a saved view
          even without an active polygon. */}
      {savedAois.length > 0 && (
        <div style={{
          position: "absolute",
          top: 52,
          left: aoiMode === "fixed" && aoiPoints.length >= 3 ? 246 : 12,
          background: "rgba(4,8,16,0.86)",
          border: "1px solid rgba(255,255,255,0.18)",
          borderRadius: 4,
          padding: "6px 8px",
          fontFamily: "'IBM Plex Mono', monospace",
          fontSize: 10,
          color: "rgba(255,255,255,0.85)",
          zIndex: 2,
          minWidth: 140,
        }}>
          <button
            type="button"
            onClick={() => setSavedAoisPickerOpen(o => !o)}
            style={{
              appearance: "none",
              background: "transparent",
              border: "none",
              color: "rgba(255,255,255,0.7)",
              fontFamily: "inherit",
              fontSize: 9,
              letterSpacing: "0.16em",
              textTransform: "uppercase",
              cursor: "pointer",
              padding: 0,
              display: "flex",
              alignItems: "center",
              gap: 6,
              width: "100%",
              justifyContent: "space-between",
            }}
          >
            <span>📁 saved AOIs · {savedAois.length}</span>
            <span>{savedAoisPickerOpen ? "▾" : "▸"}</span>
          </button>
          {savedAoisPickerOpen && (
            <div style={{ marginTop: 6, display: "flex",
                          flexDirection: "column", gap: 2 }}>
              {savedAois.map((aoi) => (
                <div key={aoi.id} style={{ display: "flex", gap: 4 }}>
                  <button
                    type="button"
                    onClick={() => {
                      setAoiPoints(aoi.points.slice());
                      setAoiMode("fixed");
                      setSavedAoisPickerOpen(false);
                    }}
                    title={`${aoi.points.length} vertices · saved ${aoi.created_at?.slice(0, 16) || ""}`}
                    style={{
                      flex: 1,
                      appearance: "none",
                      background: "rgba(255,255,255,0.04)",
                      border: "1px solid rgba(255,255,255,0.15)",
                      color: "#f0f4ff",
                      fontFamily: "inherit",
                      fontSize: 10,
                      padding: "3px 6px",
                      cursor: "pointer",
                      textAlign: "left",
                      borderRadius: 2,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    ↺ {aoi.name}
                  </button>
                  <button
                    type="button"
                    onClick={() => persistSavedAois(
                      savedAois.filter((x) => x.id !== aoi.id))}
                    title="Delete preset"
                    style={{
                      appearance: "none",
                      background: "transparent",
                      border: "1px solid rgba(255,255,255,0.15)",
                      color: "rgba(255,255,255,0.5)",
                      fontFamily: "inherit",
                      fontSize: 10,
                      padding: "3px 6px",
                      cursor: "pointer",
                      borderRadius: 2,
                    }}
                  >×</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Anomaly banner — top-center. Shows a chip per anomaly category
          with live counts. Click any chip to fit-bounds-zoom to those
          entities. Pure derived state from the entities prop — no
          additional fetches. */}
      {renderEntities.length > 0 && (
        <AnomalyBanner
          entities={renderEntities}
          cfg={cfg}
          onZoomTo={(type) => {
            const map = mapRef.current;
            if (!map) return;
            // Synthetic 'convoy' type — matches any entity with attrs.convoy_id.
            // Convoys aren't an entity type per se but a contextual grouping,
            // so the chip filter is a different predicate.
            const matches = type === "convoy"
              ? renderEntities.filter((e) => e.attrs && e.attrs.convoy_id)
              : renderEntities.filter((e) => e.type === type);
            if (matches.length === 0) return;
            if (matches.length === 1) {
              map.flyTo({ center: [matches[0].lon, matches[0].lat], zoom: 11, duration: 800 });
              onSelect(matches[0].id);
              return;
            }
            const lons = matches.map((e) => e.lon);
            const lats = matches.map((e) => e.lat);
            const minLon = Math.min(...lons), maxLon = Math.max(...lons);
            const minLat = Math.min(...lats), maxLat = Math.max(...lats);
            map.fitBounds(
              [[minLon, minLat], [maxLon, maxLat]],
              { padding: 80, duration: 800, maxZoom: 11 },
            );
          }}
        />
      )}

      {/* Toast stream — top-right corner. Transient cards announcing
          new anomaly transitions (vessel → dark_vessel etc). Auto-dismiss
          after 6 s, max 5 visible. Plus a small mute toggle for the
          dark-vessel audio ping. */}
      <ToastStack
        toasts={toasts}
        soundEnabled={soundEnabled}
        onToggleSound={() => persistSound(!soundEnabled)}
        onClick={(t) => onSelect(t.entity_id)}
        cfg={cfg}
      />

      {/* System pulse — single-glance freshness panel, bottom-right.
          Polls /maritime/realtime every 30 s. Each sensor row shows
          a colored dot keyed to "how stale is this", a label, and a
          short numeric detail. Lets the operator tell instantly
          whether AIS / SAR / buoys / etc are alive.

          Also pushes a toast into the corner stream on any
          fresh→stale or stale→down transition so the operator
          can't miss a feed degrading. */}
      <SystemPulse
        apiBase={API_BASE}
        onSensorAlarm={(alarm) => {
          setToasts((cur) => {
            const t = {
              id: `sensor_${alarm.key}_${Date.now()}`,
              entity_id: null,
              name: `${alarm.label} feed`,
              mmsi: null,
              from: alarm.from,
              to: alarm.to === "down" ? "DOWN" : "STALE",
              meta: {
                color: alarm.to === "down" ? "#e0556e" : "#f0a830",
                label: alarm.to === "down" ? "SENSOR DOWN" : "SENSOR STALE",
              },
              ts: Date.now(),
            };
            return [t, ...cur].slice(0, TOAST_MAX_VISIBLE);
          });
          if (soundEnabled && alarm.to === "down") _playDarkVesselPing();
        }}
      />

      {/* Time-scrub slider. Bottom-center, full width. Slider value is
          minutes-into-the-past (0 = now). Operators slide back to see
          where vessels were earlier. Hidden if entities is empty so the
          map looks clean before data lands. */}
      {renderEntities.length > 0 && (
        <ScrubBar
          minutes={scrubMinutes}
          loading={scrubLoading}
          live={scrubMinutes === 0}
          onChange={setScrubMinutes}
          onLive={() => setScrubMinutes(0)}
        />
      )}
    </div>
  );
}

// AnomalyBanner — top-center chip strip showing live counts per anomaly
// type. Each chip is keyed to typeMeta.color for visual continuity with
// the markers on the map. Click a chip to zoom to those entities.
//
// Why these specific types: they're the four maritime anomaly classes
// in fusion.py — dark_vessel (no AIS), ais_gap (lost AIS), loitering_vessel
// (AIS but stationary >threshold), ais_spoofed (AIS but lying). Together
// they describe every way a real-world target deviates from "normal AIS
// transponder reporting position truthfully."
//
// "vessel" itself is shown last as a neutral count so the operator sees
// the denominator — useful at a glance to know e.g. "5 dark out of 264
// total" rather than "5 dark in some unknown fleet size."
function AnomalyBanner({ entities, cfg, onZoomTo }) {
  // Order matters — most operationally-urgent first.
  const anomalyTypes = [
    "dark_vessel",
    "ais_spoofed",
    "port_skipping",
    "loitering_vessel",
    "ais_gap",
  ];
  const counts = {};
  for (const t of anomalyTypes) counts[t] = 0;
  let totalVessels = 0;
  // Convoys: count unique attrs.convoy_id across entities, and remember
  // the head member of each so the operator can click → fit-bounds-zoom
  // to the formation.
  const convoyMembers = new Map();   // convoy_id → [{lon, lat, id}, ...]
  for (const e of entities) {
    if (e.type in counts) counts[e.type] += 1;
    if (e.type === "vessel") totalVessels += 1;
    const cid = e.attrs && e.attrs.convoy_id;
    if (cid) {
      if (!convoyMembers.has(cid)) convoyMembers.set(cid, []);
      convoyMembers.get(cid).push(e);
    }
  }
  const nConvoys = convoyMembers.size;
  const totalAnomalies = anomalyTypes.reduce((s, t) => s + counts[t], 0);

  return (
    <div
      style={{
        position: "absolute",
        // Dropped from top: 8 → top: 52 so the chip row sits BELOW the
        // basemap / SAR / S2 / BUOY / GOES / AIS / VIIRS toggle bar that
        // also lives at top: 8 — previously the centered banner overlapped
        // the right half of the toggles when the window was narrower than
        // ~1500 px.
        top: 52,
        left: "50%",
        transform: "translateX(-50%)",
        display: "flex",
        gap: 6,
        background: "rgba(4,8,16,0.78)",
        border: "1px solid rgba(255,255,255,0.18)",
        borderRadius: 4,
        padding: "5px 6px",
        fontFamily:
          "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 11,
        letterSpacing: "0.06em",
        zIndex: 1,
      }}
    >
      {anomalyTypes.map((t) => {
        const meta = cfg.typeMeta[t];
        if (!meta) return null;
        const n = counts[t];
        const dim = n === 0;
        return (
          <button
            key={t}
            type="button"
            onClick={() => onZoomTo(t)}
            disabled={n === 0}
            title={
              n === 0
                ? `No ${meta.label.toLowerCase()} right now`
                : `Zoom to ${n} ${meta.label.toLowerCase()}`
            }
            style={{
              appearance: "none",
              background: dim ? "transparent" : `${meta.color}26`, // 15% alpha
              border: `1px solid ${dim ? "rgba(255,255,255,0.10)" : meta.color}`,
              color: dim ? "rgba(255,255,255,0.4)" : "#f0f4ff",
              borderRadius: 3,
              padding: "4px 8px",
              cursor: n === 0 ? "default" : "pointer",
              textTransform: "uppercase",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: meta.color,
                opacity: dim ? 0.4 : 1,
                boxShadow: dim ? "none" : `0 0 6px ${meta.color}`,
              }}
            />
            <span style={{ fontVariantNumeric: "tabular-nums",
                           color: dim ? "inherit" : meta.color,
                           fontWeight: 600 }}>
              {n}
            </span>
            <span>{meta.label}</span>
          </button>
        );
      })}
      {/* Convoy chip — distinct color (cyan, matches the on-map line)
          since convoys aren't an entity-type anomaly per se but a
          contextual formation signal. Click → zoom to convoy members. */}
      <button
        type="button"
        onClick={() => {
          if (nConvoys === 0) return;
          onZoomTo("convoy");
        }}
        disabled={nConvoys === 0}
        title={nConvoys === 0
          ? "No active convoys"
          : `${nConvoys} convoy${nConvoys === 1 ? "" : "s"} in formation`}
        style={{
          appearance: "none",
          background: nConvoys === 0 ? "transparent" : "rgba(93,214,196,0.18)",
          border: `1px solid ${nConvoys === 0 ? "rgba(255,255,255,0.10)" : "#5dd6c4"}`,
          color: nConvoys === 0 ? "rgba(255,255,255,0.4)" : "#f0f4ff",
          borderRadius: 3,
          padding: "4px 8px",
          cursor: nConvoys === 0 ? "default" : "pointer",
          textTransform: "uppercase",
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <span style={{
          width: 7, height: 7, borderRadius: "50%",
          background: "#5dd6c4",
          opacity: nConvoys === 0 ? 0.4 : 1,
          boxShadow: nConvoys === 0 ? "none" : "0 0 6px #5dd6c4",
        }} />
        <span style={{
          fontVariantNumeric: "tabular-nums",
          color: nConvoys === 0 ? "inherit" : "#5dd6c4",
          fontWeight: 600,
        }}>
          {nConvoys}
        </span>
        <span>CONVOY{nConvoys === 1 ? "" : "S"}</span>
      </button>
      <div
        style={{
          alignSelf: "center",
          padding: "0 8px 0 4px",
          color: "rgba(255,255,255,0.55)",
          borderLeft: "1px solid rgba(255,255,255,0.18)",
          marginLeft: 2,
        }}
      >
        <span style={{ fontVariantNumeric: "tabular-nums" }}>{totalAnomalies}</span>
        {" / "}
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {totalVessels + totalAnomalies}
        </span>
        {" anomalous"}
      </div>
      <button
        type="button"
        onClick={() => _downloadAnomaliesCsv(entities, anomalyTypes)}
        disabled={totalAnomalies === 0}
        title={totalAnomalies === 0
          ? "No anomalies to export"
          : `Download ${totalAnomalies} anomalies as CSV`}
        style={{
          appearance: "none",
          background: totalAnomalies === 0 ? "transparent" : "rgba(255,255,255,0.04)",
          border: `1px solid ${totalAnomalies === 0
            ? "rgba(255,255,255,0.10)" : "rgba(255,255,255,0.3)"}`,
          color: totalAnomalies === 0 ? "rgba(255,255,255,0.4)" : "#f0f4ff",
          borderRadius: 3,
          padding: "4px 8px",
          cursor: totalAnomalies === 0 ? "default" : "pointer",
          textTransform: "uppercase",
          fontFamily: "'IBM Plex Mono', monospace",
          fontSize: 10,
          letterSpacing: "0.12em",
          fontWeight: 600,
        }}
      >
        ⬇ csv
      </button>
      <button
        type="button"
        onClick={() => {
          // Browser handles the download via the Content-Disposition
          // header the backend sets. Opening in a new tab keeps the
          // current Workbench state intact.
          window.open(`${API_BASE}/maritime/daily_brief.pdf`, "_blank");
        }}
        title="Download today's PDF brief — anomaly tally + dispatch log + audit hash"
        style={{
          appearance: "none",
          background: "rgba(255,255,255,0.04)",
          border: "1px solid rgba(255,255,255,0.3)",
          color: "#f0f4ff",
          borderRadius: 3,
          padding: "4px 8px",
          cursor: "pointer",
          textTransform: "uppercase",
          fontFamily: "'IBM Plex Mono', monospace",
          fontSize: 10,
          letterSpacing: "0.12em",
          fontWeight: 600,
        }}
      >
        ⬇ pdf
      </button>
    </div>
  );
}

// CSV export — dumps the current anomaly entities to a downloadable
// file. Useful for the "send me a report" demo moment and as a real
// operational tool (operators routinely paste these into briefings).
// Client-side only via Blob+URL.createObjectURL; no backend trip.
function _downloadAnomaliesCsv(entities, anomalyTypes) {
  const anomSet = new Set(anomalyTypes);
  const rows = entities.filter((e) => anomSet.has(e.type) ||
                                       (e.attrs && e.attrs.convoy_id));
  if (rows.length === 0) return;
  const headers = [
    "entity_id", "type", "name", "mmsi", "lon", "lat",
    "first_seen", "last_seen", "speed_kn", "heading",
    "priority", "confidence", "convoy_id", "loitering_hours",
    "spoof_event_count", "notes",
  ];
  const escape = (v) => {
    if (v == null) return "";
    const s = String(v);
    if (/[,"\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const lines = [headers.join(",")];
  for (const e of rows) {
    const a = e.attrs || {};
    lines.push([
      e.id, e.type, e.name || "", a.mmsi || "",
      e.lon, e.lat,
      e.first_seen || "", e.last_seen || "",
      a.speed_kn ?? "", a.heading ?? "",
      e.priority ?? "", e.confidence ?? "",
      a.convoy_id || "",
      a.loitering_hours ?? "",
      (a.spoof_events && a.spoof_events.length) || "",
      (e.notes || "").replace(/\n/g, " "),
    ].map(escape).join(","));
  }
  const csv = lines.join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const a = document.createElement("a");
  a.href = url;
  a.download = `semper-safe-anomalies-${stamp}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Free the object-URL on the next tick — safe since the browser
  // already initiated the download.
  setTimeout(() => URL.revokeObjectURL(url), 250);
}

// ToastStack — corner notification stream. Surfaces new anomaly
// transitions (vessel → dark/spoofed/loitering) in transient cards so
// the operator can spot live state changes without staring at the
// counter bar. Position: top-right, BELOW the entity-detail panel
// header so it doesn't fight with the detail panel for space. The
// stack stays slim (max 5 visible, auto-dismiss after 6 s) so it
// can never wallpaper the map.
function ToastStack({ toasts, soundEnabled, onToggleSound, onClick, cfg }) {
  if (toasts.length === 0 && soundEnabled) {
    // No active toasts AND sound is on: render nothing (keeps the
    // corner quiet when the demo has nothing to say). When sound is
    // muted we still show a tiny "muted" pill so the operator
    // doesn't forget the toggle exists.
    return null;
  }
  return (
    <div style={{
      position: "absolute",
      top: 52,
      right: 12,
      display: "flex",
      flexDirection: "column",
      gap: 6,
      maxWidth: 280,
      zIndex: 2,
      pointerEvents: "none",   // let map clicks through except on cards
    }}>
      {!soundEnabled && (
        <button
          type="button"
          onClick={onToggleSound}
          title="Unmute dark-vessel sound alerts"
          style={{
            pointerEvents: "auto",
            alignSelf: "flex-end",
            appearance: "none",
            border: "1px solid rgba(255,255,255,0.18)",
            background: "rgba(4,8,16,0.78)",
            color: "rgba(255,255,255,0.55)",
            borderRadius: 3,
            padding: "3px 8px",
            fontSize: 10,
            fontFamily: "'IBM Plex Mono', monospace",
            letterSpacing: "0.12em",
            cursor: "pointer",
            textTransform: "uppercase",
          }}
        >
          🔇 muted
        </button>
      )}
      {toasts.map((t) => {
        const meta = t.meta || cfg.typeMeta[t.to] || {};
        const label = meta.label || t.to;
        const color = meta.color || "#5fd093";
        return (
          <button
            key={t.id}
            type="button"
            onClick={() => onClick && onClick(t)}
            style={{
              pointerEvents: "auto",
              appearance: "none",
              textAlign: "left",
              background: "rgba(4,8,16,0.88)",
              border: `1px solid ${color}`,
              borderLeft: `4px solid ${color}`,
              borderRadius: 4,
              padding: "8px 10px",
              cursor: "pointer",
              fontFamily: "'IBM Plex Sans', sans-serif",
              color: "#f0f4ff",
              display: "flex",
              flexDirection: "column",
              gap: 2,
              boxShadow: `0 4px 12px rgba(0,0,0,0.4)`,
              animation: "ss-toast-in 220ms ease-out",
            }}
          >
            <div style={{
              fontFamily: "'IBM Plex Mono', monospace",
              fontSize: 9,
              letterSpacing: "0.16em",
              color: color,
              fontWeight: 600,
            }}>
              {t.from ? `${t.from.toUpperCase()} → ${label}` : `NEW · ${label}`}
            </div>
            <div style={{ fontSize: 13, fontWeight: 500 }}>
              {t.name || (
                <span style={{ fontStyle: "italic", opacity: 0.7 }}>
                  unidentified
                </span>
              )}
              {t.mmsi && (
                <span style={{
                  fontFamily: "'IBM Plex Mono', monospace",
                  fontSize: 11, marginLeft: 8, opacity: 0.6,
                }}>
                  MMSI {t.mmsi}
                </span>
              )}
            </div>
          </button>
        );
      })}
      {toasts.length > 0 && soundEnabled && (
        <button
          type="button"
          onClick={onToggleSound}
          title="Mute dark-vessel sound alerts"
          style={{
            pointerEvents: "auto",
            alignSelf: "flex-end",
            appearance: "none",
            border: "1px solid rgba(255,255,255,0.10)",
            background: "transparent",
            color: "rgba(255,255,255,0.45)",
            borderRadius: 3,
            padding: "1px 6px",
            fontSize: 9,
            fontFamily: "'IBM Plex Mono', monospace",
            letterSpacing: "0.12em",
            cursor: "pointer",
            textTransform: "uppercase",
          }}
        >
          🔊 sound on · mute
        </button>
      )}
    </div>
  );
}

// Replay knobs: start position and total animation duration. 60 minutes
// from now down to 0 over ~12 seconds gives the operator a smooth 5x
// realtime view of the last hour. Step size of 1 min == 12 ticks * 60
// ticks/sec = a tick every 200 ms — fast enough to feel like animation,
// slow enough for the /maritime/timeline backend to keep up (each call
// is ~300 ms; we let stale fetches cancel in the existing useEffect).
const REPLAY_FROM_MIN = 60;
const REPLAY_DURATION_MS = 12_000;

function ScrubBar({ minutes, loading, live, onChange, onLive }) {
  const [playing, setPlaying] = useState(false);
  const playStartRef = useRef(null);
  const rafRef = useRef(null);

  // Auto-replay: tick from REPLAY_FROM_MIN down to 0, then stop.
  // Snapping back to live at the end is the natural "the demo is over,
  // you're back in real time" gesture.
  useEffect(() => {
    if (!playing) return;
    playStartRef.current = performance.now();
    onChange(REPLAY_FROM_MIN);

    const tick = (now) => {
      const elapsed = now - playStartRef.current;
      const progress = Math.min(1, elapsed / REPLAY_DURATION_MS);
      // Linear decay from REPLAY_FROM_MIN → 0 over REPLAY_DURATION_MS.
      const m = Math.round(REPLAY_FROM_MIN * (1 - progress));
      onChange(m);
      if (progress >= 1) {
        setPlaying(false);
        // Tail of the animation lands on live (minutes=0) — no need to
        // call onLive explicitly, but do it so the "LIVE" pill flips
        // green in the UI immediately.
        onLive();
        return;
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [playing, onChange, onLive]);

  // Any manual scrub interrupts replay — feels right when an operator
  // wants to zero in on a specific moment mid-animation.
  const handleManual = (m) => {
    if (playing) setPlaying(false);
    onChange(m);
  };

  const label = live
    ? "live · now"
    : `T-${minutes} min · ${new Date(Date.now() - minutes * 60_000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;

  return (
    <div
      style={{
        position: "absolute",
        left: "50%",
        bottom: 12,
        transform: "translateX(-50%)",
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "8px 14px",
        background: "rgba(4,8,16,0.82)",
        border: "1px solid rgba(255,255,255,0.18)",
        borderRadius: 6,
        fontFamily:
          "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 11,
        letterSpacing: "0.06em",
        color: "rgba(255,255,255,0.85)",
        zIndex: 1,
        minWidth: 400,
      }}
    >
      <button
        type="button"
        onClick={onLive}
        disabled={live}
        title="snap to now"
        style={{
          appearance: "none",
          border: "1px solid rgba(255,255,255,0.25)",
          background: live ? "rgba(95,208,147,0.18)" : "rgba(255,255,255,0.04)",
          color: live ? "#cdf2dd" : "rgba(255,255,255,0.85)",
          padding: "3px 8px",
          borderRadius: 3,
          fontSize: 10,
          fontFamily: "inherit",
          letterSpacing: "0.08em",
          cursor: live ? "default" : "pointer",
          textTransform: "uppercase",
        }}
      >
        live
      </button>
      <button
        type="button"
        onClick={() => setPlaying((p) => !p)}
        title={playing ? "stop replay" : "replay last 60 minutes"}
        style={{
          appearance: "none",
          border: "1px solid rgba(255,255,255,0.25)",
          background: playing ? "rgba(240,168,48,0.22)" : "rgba(255,255,255,0.04)",
          color: playing ? "#ffd897" : "rgba(255,255,255,0.85)",
          padding: "3px 8px",
          borderRadius: 3,
          fontSize: 10,
          fontFamily: "inherit",
          letterSpacing: "0.08em",
          cursor: "pointer",
          textTransform: "uppercase",
          minWidth: 56,
        }}
      >
        {playing ? "■ stop" : "▶ replay"}
      </button>
      <input
        type="range"
        min={0}
        max={60}
        step={1}
        value={minutes}
        onChange={(e) => handleManual(Number(e.target.value))}
        // Slider is conceptually "minutes ago"; rendering it left=past,
        // right=now feels right when read like a timeline.
        style={{
          flex: 1,
          accentColor: "#5fd093",
          direction: "rtl",
        }}
      />
      <span
        style={{
          minWidth: 132,
          textAlign: "right",
          color: live ? "#5fd093" : loading ? "#f0a830" : "rgba(255,255,255,0.85)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {loading ? "loading…" : label}
      </span>
    </div>
  );
}


// ----------------------------------------------------------------------
// SystemPulse — corner widget that polls /maritime/realtime every 30 s
// and renders a small per-sensor freshness panel. Each row shows:
//   colored dot  (green ≤ fresh threshold, amber ≤ stale, red beyond)
//   label
//   age string ("12 s", "4 m", "2 h", "—" if never seen)
//   detail   small numeric extra (count, etc)
// ----------------------------------------------------------------------

const PULSE_THRESHOLDS_S = {
  ais:    { fresh: 60,        stale: 60 * 5 },         // AIS arrives every 1-2s
  buoys:  { fresh: 60 * 35,   stale: 60 * 60 * 2 },    // 30-min cadence
  sar:    { fresh: 60 * 60 * 24,    stale: 60 * 60 * 24 * 7 },  // 6-day repeat
  s2:     { fresh: 60 * 60 * 24,    stale: 60 * 60 * 24 * 7 },  // 5-day repeat
};

function _ageFmt(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86400).toFixed(1)} d`;
}

function _statusColor(seconds, thresh) {
  if (seconds == null) return "#888";
  if (seconds <= thresh.fresh) return "#5fd093";   // green
  if (seconds <= thresh.stale) return "#f0a830";   // amber
  return "#e0556e";                                // red
}

function PulseRow({ label, seconds, color, detail }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "3px 0",
    }}>
      <span style={{
        width: 8, height: 8, borderRadius: "50%",
        background: color, flex: "0 0 auto",
        boxShadow: `0 0 6px ${color}`,
      }} />
      <span style={{
        width: 44, color: "rgba(255,255,255,0.78)",
        textTransform: "uppercase", letterSpacing: "0.06em",
        fontSize: 10,
      }}>{label}</span>
      <span style={{
        width: 56, textAlign: "right", color: "rgba(255,255,255,0.92)",
        fontVariantNumeric: "tabular-nums",
      }}>{_ageFmt(seconds)}</span>
      <span style={{
        flex: 1, color: "rgba(255,255,255,0.55)",
        textAlign: "right", fontSize: 10,
      }}>{detail}</span>
    </div>
  );
}

// Track previous sensor health so we can fire a toast/sound on a
// fresh→stale transition (instead of every poll while it's stale).
function _classify(age, t) {
  if (age == null) return "unknown";
  if (age <= t.fresh) return "fresh";
  if (age <= t.stale) return "stale";
  return "down";
}

// Rolling buffer of vessel-count samples. Persisted to localStorage so
// the sparkline survives a page reload. 120 samples × 30 s = ~1 hour
// of history when the panel has been polling steadily.
const PULSE_HISTORY_KEY = "ss-pulse-history";
const PULSE_HISTORY_MAX = 120;

// Tiny inline SVG sparkline from a [{t, v}] series. Returns null
// when there are too few samples to draw anything meaningful.
function _Sparkline({ samples, color, width = 80, height = 18 }) {
  if (!samples || samples.length < 2) return null;
  const vs = samples.map((s) => s.v).filter((v) => Number.isFinite(v));
  if (vs.length < 2) return null;
  const min = Math.min(...vs);
  const max = Math.max(...vs);
  const span = Math.max(1, max - min);
  const pts = samples.map((s, i) => {
    const x = (i / (samples.length - 1)) * width;
    const y = height - ((s.v - min) / span) * (height - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}
         style={{ display: "inline-block", verticalAlign: "middle" }}>
      <polyline points={pts.join(" ")}
                fill="none" stroke={color} strokeWidth="1.2"
                strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function SystemPulse({ apiBase, onSensorAlarm }) {
  const [pulse, setPulse] = useState(null);
  const [history, setHistory] = useState(() => {
    if (typeof window === "undefined") return [];
    try {
      const raw = window.localStorage.getItem(PULSE_HISTORY_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch { return []; }
  });
  const prevHealthRef = useRef({});
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    const pull = async () => {
      try {
        const r = await fetch(`${apiBase}/maritime/realtime`, { signal: ctrl.signal });
        if (!r.ok) throw new Error(`http ${r.status}`);
        const data = await r.json();
        if (!cancelled) {
          setPulse(data);
          // Append to the rolling AIS-vessel-count history. Persist
          // every sample so the sparkline survives reloads.
          const n = data?.ais?.detail?.n_vessels;
          if (Number.isFinite(n)) {
            setHistory((cur) => {
              const next = [...cur, { t: Date.now(), v: n }];
              if (next.length > PULSE_HISTORY_MAX) {
                next.splice(0, next.length - PULSE_HISTORY_MAX);
              }
              try {
                window.localStorage.setItem(
                  PULSE_HISTORY_KEY, JSON.stringify(next));
              } catch {}
              return next;
            });
          }
        }
      } catch (err) {
        if (err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.warn("pulse fetch failed:", err);
      }
    };
    pull();
    const interval = window.setInterval(pull, 30_000);
    return () => { cancelled = true; ctrl.abort(); window.clearInterval(interval); };
  }, [apiBase]);

  // Detect fresh→stale / stale→down transitions and bubble up to the
  // host so the toast stream + audio ping can announce them. Compares
  // each sensor's current classification against the previous tick.
  useEffect(() => {
    if (!pulse || !onSensorAlarm) return;
    const sensors = [
      { key: "ais",   label: "AIS",   age: pulse.ais?.age_seconds,   t: PULSE_THRESHOLDS_S.ais },
      { key: "buoys", label: "Buoys", age: pulse.buoys?.age_seconds, t: PULSE_THRESHOLDS_S.buoys },
      { key: "sar",   label: "SAR",   age: pulse.sar?.age_seconds,   t: PULSE_THRESHOLDS_S.sar },
      { key: "s2",    label: "S2",    age: pulse.s2?.age_seconds,    t: PULSE_THRESHOLDS_S.s2 },
    ];
    const prev = prevHealthRef.current;
    for (const s of sensors) {
      const now = _classify(s.age, s.t);
      const was = prev[s.key];
      // Only alarm on degradations (fresh→stale, stale→down, fresh→down).
      // No alarm when the sensor recovers — that's good news, the UI
      // dot turning green is enough.
      if (was && now !== was) {
        const rank = { fresh: 0, stale: 1, down: 2, unknown: 3 };
        if ((rank[now] || 0) > (rank[was] || 0)) {
          onSensorAlarm({
            key: s.key, label: s.label,
            from: was, to: now, age: s.age,
          });
        }
      }
      prev[s.key] = now;
    }
  }, [pulse, onSensorAlarm]);

  if (!pulse) return null;

  const ais = pulse.ais  || {};
  const sar = pulse.sar  || {};
  const s2  = pulse.s2   || {};
  const buoys = pulse.buoys || {};
  const audit = pulse.audit || {};
  const sarDet = sar.detail?.n_detections ?? 0;
  const sarDark = sar.detail?.n_dark ?? 0;
  const sarStates = sar.detail?.scene_states || {};

  return (
    <div style={{
      position: "absolute",
      bottom: 12, right: 12,
      width: 280,
      background: "rgba(4,8,16,0.84)",
      border: "1px solid rgba(255,255,255,0.18)",
      borderRadius: 6,
      padding: "8px 12px",
      fontFamily: "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
      fontSize: 11,
      letterSpacing: "0.04em",
      color: "rgba(255,255,255,0.85)",
      zIndex: 1,
    }}>
      <div style={{
        fontSize: 10, textTransform: "uppercase", letterSpacing: "0.12em",
        color: "rgba(255,255,255,0.55)", marginBottom: 4,
      }}>
        System pulse
      </div>
      <PulseRow
        label="AIS"
        seconds={ais.age_seconds}
        color={_statusColor(ais.age_seconds, PULSE_THRESHOLDS_S.ais)}
        detail={`${ais.detail?.n_vessels ?? 0} vessels`}
      />
      {/* Vessel-count trend sparkline — only render once we have a
          meaningful window of samples. Delta compares the latest
          sample against the earliest in the buffer. */}
      {history.length >= 6 && (() => {
        const first = history[0].v;
        const last = history[history.length - 1].v;
        const deltaPct = first > 0 ? ((last - first) / first) * 100 : 0;
        const spanMin = Math.round((history[history.length - 1].t - history[0].t) / 60_000);
        const arrow = Math.abs(deltaPct) < 1.0
          ? "≈"
          : (deltaPct > 0 ? "▲" : "▼");
        const sign = deltaPct > 0 ? "+" : "";
        return (
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "0 0 4px 16px",
            fontSize: 10, color: "rgba(255,255,255,0.6)",
          }}>
            <_Sparkline samples={history} color="#5fd093" width={100} height={16} />
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {arrow} {sign}{deltaPct.toFixed(1)}%
            </span>
            <span style={{ fontSize: 9, opacity: 0.7 }}>
              · {spanMin} min
            </span>
          </div>
        );
      })()}
      <PulseRow
        label="Buoys"
        seconds={buoys.age_seconds}
        color={_statusColor(buoys.age_seconds, PULSE_THRESHOLDS_S.buoys)}
        detail={`${buoys.detail?.n_alive ?? 0}/${buoys.detail?.n_total ?? 0} live`}
      />
      <PulseRow
        label="SAR"
        seconds={sar.age_seconds}
        color={_statusColor(sar.age_seconds, PULSE_THRESHOLDS_S.sar)}
        detail={`${sarDet} det · ${sarDark} dark`}
      />
      <PulseRow
        label="S2"
        seconds={s2.age_seconds}
        color={_statusColor(s2.age_seconds, PULSE_THRESHOLDS_S.s2)}
        detail={`${(s2.detail?.scene_states?.discovered ?? 0)} scenes`}
      />
      <div style={{
        marginTop: 6, paddingTop: 6,
        borderTop: "1px solid rgba(255,255,255,0.12)",
        fontSize: 10,
        color: "rgba(255,255,255,0.55)",
        display: "flex", justifyContent: "space-between",
      }}>
        <span>audit</span>
        <span style={{
          color: "rgba(255,255,255,0.78)",
          fontVariantNumeric: "tabular-nums",
        }}>
          {audit.entries?.toLocaleString() ?? "—"} entries
        </span>
      </div>
    </div>
  );
}
