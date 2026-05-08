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

const SAR_SCENES_PATH = "/maritime/sar/scenes?limit=200";
const SAR_DETECTIONS_PATH = "/maritime/sar/detections?limit=5000";

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

export default function MapLibreView({ entities, selectedId, onSelect, cfg }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef(new Map());      // id → maplibregl.Marker
  const fitDoneRef = useRef(false);          // only auto-fit once on first data
  const [ready, setReady] = useState(false);

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
    map.on("style.load", ensureTrackLayers);
    map.on("style.load", ensureSarLayers);

    // Click → popup. Detections are densely packed so attach to the
    // detection layer specifically (otherwise the scene-fill layer
    // intercepts at low zoom and pops up the scene instead).
    const popupForDetection = (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const p = f.properties || {};
      const dark = p.matched_entity_id == null;
      const html = `
        <div style="font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;
                    font-size:11px;letter-spacing:0.04em;color:#040810;
                    min-width:200px;">
          <div style="font-weight:bold;color:${dark ? "#a02030" : "#1a6a3a"};
                      text-transform:uppercase;margin-bottom:4px;">
            ${dark ? "Dark vessel candidate" : "AIS-matched detection"}
          </div>
          <div>RCS: ${Number(p.rcs_db).toFixed(1)} dB</div>
          <div>Length: ${Number(p.length_m).toFixed(0)} m</div>
          <div>Confidence: ${Number(p.confidence).toFixed(2)}</div>
          ${p.matched_entity_id
            ? `<div>Match: <code>${p.matched_entity_id}</code></div>`
            : ""}
          <div style="margin-top:4px;color:#666;">scene ${(p.scene_id || "").slice(0, 8)}…</div>
        </div>
      `;
      new maplibregl.Popup({ closeButton: true, maxWidth: "260px" })
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
    map.on("click", SAR_DETECTIONS_LAYER_ID, popupForDetection);
    map.on("click", SAR_SCENES_FILL_LAYER_ID, popupForScene);
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

      el.innerHTML = `
        <svg viewBox="-10 -10 20 20" width="16" height="16"
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
                  : `<circle r="${r}" fill="${meta.color}"
                              stroke="${stroke}" stroke-width="${strokeWidth}"/>`}
        </svg>
      `;
      el.title = `${meta.label || e.type}${e.name ? ` — ${e.name}` : ""}`;
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
      </div>

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

function ScrubBar({ minutes, loading, live, onChange, onLive }) {
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
        minWidth: 360,
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
      <input
        type="range"
        min={0}
        max={60}
        step={1}
        value={minutes}
        onChange={(e) => onChange(Number(e.target.value))}
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
