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

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

const TRACK_SOURCE_ID = "ss-selected-track";
const TRACK_LAYER_ID = "ss-selected-track-line";
const TRACK_HEAD_LAYER_ID = "ss-selected-track-head";

async function fetchTrack(apiPath, eid, signal) {
  const r = await fetch(`${API_BASE}${apiPath}/entities/${eid}/track?limit=200`, { signal });
  if (!r.ok) throw new Error(`track ${r.status}`);
  return r.json();
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
    map.on("style.load", ensureTrackLayers);
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
  // Sync markers whenever entities change. Reuse existing marker DOM
  // when the entity is unchanged so we don't churn through 1000s of
  // create/destroys every time AISStream pushes a new observation.
  // ------------------------------------------------------------------
  const entitiesById = useMemo(() => {
    const m = new Map();
    for (const e of entities) m.set(e.id, e);
    return m;
  }, [entities]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    const liveIds = new Set();

    for (const e of entities) {
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
    // alone so refreshes don't snap them out of context.
    if (!fitDoneRef.current && entities.length > 0) {
      const bounds = new maplibregl.LngLatBounds();
      let any = false;
      for (const e of entities) {
        if (typeof e.lon === "number" && typeof e.lat === "number") {
          bounds.extend([e.lon, e.lat]);
          any = true;
        }
      }
      if (any) {
        map.fitBounds(bounds, { padding: FIT_PADDING, duration: 0, maxZoom: 9 });
        fitDoneRef.current = true;
      }
    }
  }, [entities, entitiesById, selectedId, cfg, ready, onSelect]);

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
        const coords = (data.track || [])
          .filter((p) => typeof p.lon === "number" && typeof p.lat === "number")
          .map((p) => [p.lon, p.lat]);
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
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.warn("track fetch failed:", err);
        src.setData({ type: "FeatureCollection", features: [] });
      });

    return () => ctrl.abort();
  }, [selectedId, ready, entitiesById, cfg]);

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
      </div>
    </div>
  );
}
