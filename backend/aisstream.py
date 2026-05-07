"""
AISStream.io ingestion worker (Phase 2).

Subscribes to AISStream's WebSocket feed for a bounding box, normalizes each
message into the platform's `Observation` schema, and feeds it into the
maritime FusionEngine. The engine handles entity creation, dark-vessel
detection, AIS-gap detection — same plumbing the synthetic seed scenarios
use, just driven by real-time data instead.

Runs as an asyncio background task inside the FastAPI process so the in-memory
engine state is the single source of truth for the API. The alternative
(separate worker process talking to the same Postgres) creates two-process
coherence problems that don't pay for themselves at this scale.

Reference:
  - AISStream docs: https://aisstream.io/documentation
  - Bounding box format: [[SW_lat, SW_lon], [NE_lat, NE_lon]]
  - Two relevant message types:
      * PositionReport — lat, lon, course, speed, navigational status
      * ShipStaticData — name, callsign, dimensions, destination, ETA

Failure modes handled:
  - Connection drops → exponential backoff reconnect
  - Bad JSON / unexpected schema → log + skip the message
  - Engine ingest exception → log + skip the message
  - API key missing or env unset → worker doesn't start (caller checks)

Reading order: __init__ at the bottom for the run loop, the parsers up top.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import h3
import websockets

from models import Geom, Observation, SourceType


log = logging.getLogger("aisstream")


WS_URL = "wss://stream.aisstream.io/v0/stream"
H3_RES = 8                          # roadmap pinned resolution
RECONNECT_BACKOFF_S = (1, 2, 4, 8, 15, 30, 60)


# Texas shoreline AOI per memory/semper_safe_aoi.md.
# AISStream wants [[SW_lat, SW_lon], [NE_lat, NE_lon]].
TEXAS_SHORELINE_BBOX: list[list[list[float]]] = [
    [[25.5, -98.0], [30.5, -93.5]],
]


# AISStream serializes timestamps as Go's time.Time default String():
#   "2026-05-07 22:44:23.622076095 +0000 UTC"
# Python's strptime can't parse the trailing " UTC" cleanly, so peel it.
_GO_UTC_RE = re.compile(r"^(?P<base>.+?)(?:\s+\+\d{4})?(?:\s+UTC)?$")


def _parse_go_utc(s: str) -> datetime:
    m = _GO_UTC_RE.match(s.strip())
    base = m.group("base") if m else s
    # Trim sub-microsecond precision that strptime can't handle.
    if "." in base:
        head, frac = base.rsplit(".", 1)
        frac = "".join(ch for ch in frac if ch.isdigit())[:6]
        base = f"{head}.{frac}" if frac else head
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in base else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(base, fmt).replace(tzinfo=timezone.utc)


def _clean_ais_string(s: Any) -> str:
    """AIS pads strings with trailing spaces and ASCII '@'. Strip them."""
    if not isinstance(s, str):
        return ""
    return s.replace("@", "").strip()


# --- Normalization -----------------------------------------------------

def position_report_to_observation(msg: dict[str, Any]) -> Observation | None:
    """Convert a PositionReport JSON message to an Observation. Returns None
    if required fields are missing — AISStream occasionally emits partial
    messages and we don't want one bad row to crash ingestion."""
    md = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get("PositionReport") or {}

    mmsi = md.get("MMSI") or md.get("MMSI_String")
    lat = md.get("latitude")
    lon = md.get("longitude")
    t_str = md.get("time_utc")
    if mmsi is None or lat is None or lon is None or not t_str:
        return None

    try:
        t = _parse_go_utc(t_str)
    except (ValueError, AttributeError):
        return None

    cell = h3.latlng_to_cell(float(lat), float(lon), H3_RES)

    attrs: dict[str, Any] = {
        "mmsi": str(mmsi),
        "name": _clean_ais_string(md.get("ShipName")) or None,
        "type": "vessel",
    }
    # Optional position-report fields. Missing ones stay out of attrs.
    for src_key, dst_key in (
        ("Cog", "heading"),
        ("Sog", "speed_kn"),
        ("NavigationalStatus", "nav_status"),
        ("TrueHeading", "true_heading"),
        ("RateOfTurn", "rate_of_turn"),
    ):
        v = body.get(src_key)
        if v is not None:
            attrs[dst_key] = v

    return Observation(
        obs_id=f"obs_{uuid.uuid4().hex[:12]}",
        source=SourceType.AIS,
        source_id=str(mmsi),
        geom=Geom(lon=float(lon), lat=float(lat)),
        h3_cell=cell,
        t=t,
        attrs=attrs,
        raw_lineage="aisstream.io",
    )


def ship_static_data_attrs(msg: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Pull the durable ship-identity fields. Returns (mmsi, attrs_to_merge)
    or None if unparseable. Caller merges attrs into the existing entity."""
    md = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get("ShipStaticData") or {}
    mmsi = md.get("MMSI") or md.get("MMSI_String")
    if mmsi is None:
        return None

    attrs: dict[str, Any] = {}
    name = _clean_ais_string(md.get("ShipName")) or _clean_ais_string(body.get("Name"))
    if name:
        attrs["name"] = name
    for src_key, dst_key in (
        ("CallSign", "call_sign"),
        ("Destination", "destination"),
        ("ImoNumber", "imo"),
        ("Type", "ship_type"),
    ):
        v = body.get(src_key)
        if v not in (None, "", 0):
            attrs[dst_key] = (
                _clean_ais_string(v) if isinstance(v, str) else v
            )
    dim = body.get("Dimension") or {}
    if dim:
        # AIS dimensions: A=bow→reference, B=ref→stern, C=port→ref, D=ref→starboard
        length = (dim.get("A") or 0) + (dim.get("B") or 0)
        beam = (dim.get("C") or 0) + (dim.get("D") or 0)
        if length:
            attrs["length_m"] = length
        if beam:
            attrs["beam_m"] = beam

    return (str(mmsi), attrs) if attrs else None


# --- Worker ------------------------------------------------------------

async def _consume(ws, on_observation: Callable[[Observation], Awaitable[None]],
                   on_static: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("non-JSON message from aisstream; skipping")
            continue

        mt = msg.get("MessageType")
        if mt == "PositionReport":
            obs = position_report_to_observation(msg)
            if obs is not None:
                try:
                    await on_observation(obs)
                except Exception as exc:  # noqa: BLE001
                    log.exception("on_observation crashed for mmsi=%s: %s",
                                  obs.source_id, exc)
        elif mt == "ShipStaticData":
            parsed = ship_static_data_attrs(msg)
            if parsed is not None:
                mmsi, attrs = parsed
                try:
                    await on_static(mmsi, attrs)
                except Exception as exc:  # noqa: BLE001
                    log.exception("on_static crashed for mmsi=%s: %s", mmsi, exc)


async def run_worker(
    *,
    api_key: str,
    bbox: list[list[list[float]]] | None = None,
    on_observation: Callable[[Observation], Awaitable[None]],
    on_static: Callable[[str, dict[str, Any]], Awaitable[None]],
    cancel: asyncio.Event | None = None,
) -> None:
    """Subscribe and feed messages forever. Reconnects on disconnect.

    Caller passes async callbacks. on_observation receives a fully-formed
    Pydantic Observation; on_static receives (mmsi, attrs_dict_to_merge).
    """
    if not api_key:
        raise ValueError("AISSTREAM_API_KEY is empty")

    sub = {
        "APIKey": api_key,
        "BoundingBoxes": bbox or TEXAS_SHORELINE_BBOX,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    attempt = 0
    while cancel is None or not cancel.is_set():
        try:
            async with websockets.connect(WS_URL, max_size=1_000_000) as ws:
                await ws.send(json.dumps(sub))
                attempt = 0
                log.info("aisstream connected; bbox=%s", sub["BoundingBoxes"])
                await _consume(ws, on_observation, on_static)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            backoff = RECONNECT_BACKOFF_S[
                min(attempt, len(RECONNECT_BACKOFF_S) - 1)
            ]
            log.warning("aisstream disconnected (%s); retry in %ds",
                        type(exc).__name__, backoff)
            attempt += 1
            try:
                await asyncio.wait_for(
                    cancel.wait() if cancel else asyncio.sleep(backoff),
                    timeout=backoff,
                )
                if cancel and cancel.is_set():
                    return
            except asyncio.TimeoutError:
                pass
