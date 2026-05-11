"""
NWS active-alerts client for fire-weather products.

The National Weather Service publishes active alerts as GeoJSON at
api.weather.gov/alerts/active. Filtered queries by event type let us
pull just Red Flag Warnings (RFW) and Fire Weather Watches (FWW) —
the two products that should drive a prevention dashboard.

Public, no key, requires a descriptive User-Agent. Polygons come back
as multi-polygons; we flatten to a list of features ready for the
frontend.
"""

from __future__ import annotations

import logging

log = logging.getLogger("nws_alerts")

_ALERTS_URL = "https://api.weather.gov/alerts/active"

# Two fire-weather products. NWS uses these exact event-type strings;
# they're case-sensitive in the API filter.
_FIRE_WEATHER_EVENTS = ["Red Flag Warning", "Fire Weather Watch"]

# Reasonable User-Agent so NWS doesn't reject the request. Their
# guidance is "include contact info" — we identify the app.
_USER_AGENT = "semper-safe/0.1 (https://sempersafe.live; ops@sempersafe.live)"


def fetch_red_flag_warnings() -> list[dict]:
    """Return active Red Flag + Fire Weather Watch polygons.

    Each element:
      {
        id, event, severity, urgency, certainty,
        effective, expires, headline,
        geometry: GeoJSON Polygon/MultiPolygon,
      }
    NWS attaches polygons selectively — many alerts only carry an
    affectedZones list, no geometry. We skip those (the operator's
    polygon overlay would render nothing).
    """
    import httpx

    out: list[dict] = []
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
    for event in _FIRE_WEATHER_EVENTS:
        try:
            with httpx.Client(timeout=20.0, headers=headers) as client:
                r = client.get(_ALERTS_URL, params={"event": event})
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("NWS alerts fetch failed for %s: %s", event, exc)
            continue
        for feat in data.get("features", []):
            geom = feat.get("geometry")
            if not geom:
                continue  # no polygon attached — skip
            props = feat.get("properties") or {}
            out.append({
                "id":          feat.get("id") or props.get("id"),
                "event":       props.get("event") or event,
                "severity":    props.get("severity"),
                "urgency":     props.get("urgency"),
                "certainty":   props.get("certainty"),
                "effective":   props.get("effective"),
                "expires":     props.get("expires"),
                "ends":        props.get("ends"),
                "headline":    props.get("headline"),
                "sender":      props.get("senderName"),
                "geometry":    geom,
            })
    return out
