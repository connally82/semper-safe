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

// OpenFreeMap dark style — fully free, no API key, vector tiles.
// Falls back to a minimal gray raster if OFM is ever down.
const STYLE_URL = "https://tiles.openfreemap.org/styles/dark";

// Default view: Texas shoreline AOI center + zoom that shows Galveston Bay
// to Brownsville on a typical screen.
const DEFAULT_CENTER = [-95.5, 28.5];
const DEFAULT_ZOOM = 5.5;

const FIT_PADDING = 60;

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

  // ------------------------------------------------------------------
  // Initialize the map exactly once.
  // ------------------------------------------------------------------
  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: STYLE_URL,
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
      attributionControl: { compact: true },
      // Wheel zoom but no rotation/pitch — operators don't need 3D.
      pitchWithRotate: false,
      dragRotate: false,
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "nautical" }), "bottom-left");

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

  return (
    <div
      ref={containerRef}
      style={{
        position: "relative",
        flex: 1,
        background: "#040810",
        overflow: "hidden",
        minHeight: 0,
      }}
    />
  );
}
