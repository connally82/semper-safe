"""
Email alerts (Phase 3a of docs/roadmap.md).

Why this exists:
  An operator who has to keep a browser tab pinned to a map all day to
  notice a dark vessel will not actually do that. The alert path turns
  Semper Safe from "look at the map" into "the right people get told
  when something interesting shows up".

Design:
  - Resend HTTP API for delivery. Single POST per alert. No SDK needed
    (httpx is already a backend dep for Copernicus).
  - Subscribers come from the ALERT_SUBSCRIBERS env var
    (comma-separated emails) for the MVP. Phase 3b moves them to a
    DB table tied to Clerk user IDs + per-user AOI filters.
  - All alert sends are best-effort and audit-logged — if Resend is
    unconfigured or rate-limited, we log + continue. The detection
    pipeline never fails because an email did not go out.
  - HTML body is built locally; no external template engine. Keeps
    the cold-start small and the debug surface inside this file.
  - Severity threshold gates the noise: only newly-classified dark
    vessels above MIN_CONFIDENCE produce alerts. Confidence comes
    from the CFAR detector's RCS-driven sigmoid (see sar_processor).

What this version does NOT do:
  - Per-user AOI filtering. Today every subscriber gets every alert.
  - Throttling / digesting. A scene with 50 detections sends ONE email
    that lists the top N — but multiple scenes processed in quick
    succession produce multiple emails. Phase 3b adds windowed
    digesting via a per-subscriber outbox.
  - Acknowledgements / unsubscribe links. Pre-Clerk, identifying a
    user from a click-through token is its own scope.
  - SMS / push. Email-first because it's universal.

Reference: https://resend.com/docs/api-reference/emails/send-email
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("alerts")

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Tunables — defaults match the operator notes in docs/blueprint.md.
MIN_CONFIDENCE = 0.55      # dark vessels under this aren't alert-worthy
MAX_DETECTIONS_IN_BODY = 8 # cap the email body — link to UI for the rest
DEFAULT_FROM = "Semper Safe <alerts@sempersafe.live>"
DEFAULT_SUBJECT_PREFIX = "[Semper Safe]"


# --- config --------------------------------------------------------------

def _resend_config() -> dict[str, str] | None:
    """Return Resend settings if configured, else None.

    None is the explicit "alerts disabled" signal — every send_*
    function returns early on None instead of erroring. Lets the rest
    of the platform run normally before the user wires up Resend.
    """
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "from": (os.environ.get("RESEND_FROM") or DEFAULT_FROM).strip(),
    }


def is_configured() -> bool:
    return _resend_config() is not None


def subscribers() -> list[str]:
    """Comma-separated email list from ALERT_SUBSCRIBERS. Empty → no recipients.

    Allows you to flip alerts off by clearing the env var without redeploying
    code. Phase 3b replaces this with a DB-backed subscribers table.
    """
    raw = (os.environ.get("ALERT_SUBSCRIBERS") or "").strip()
    if not raw:
        return []
    return [e.strip() for e in raw.split(",") if e.strip()]


# --- low-level send ------------------------------------------------------

def send_email(*, to: list[str], subject: str, html: str,
               text: str | None = None) -> dict[str, Any]:
    """POST one email to Resend. Returns the parsed Resend response on
    success, or a {"skipped": "..."} dict if alerts are unconfigured.

    Caller is responsible for catching exceptions and audit-logging.
    """
    cfg = _resend_config()
    if cfg is None:
        return {"skipped": "RESEND_API_KEY not set"}
    if not to:
        return {"skipped": "no recipients"}

    payload = {
        "from": cfg["from"],
        "to": to,
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    r = httpx.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# --- alert builders ------------------------------------------------------

def _format_detection_row(d: dict[str, Any]) -> str:
    lat = d.get("lat")
    lon = d.get("lon")
    rcs = d.get("rcs_db")
    length = d.get("length_m")
    conf = d.get("confidence")
    return (
        f'<tr style="border-top:1px solid #2a2f3a;">'
        f'  <td style="padding:6px 10px;font-variant-numeric:tabular-nums;'
        f'             font-family:ui-monospace,Menlo,monospace;">'
        f'    {lat:.4f}°N {lon:.4f}°W'
        f'  </td>'
        f'  <td style="padding:6px 10px;text-align:right;'
        f'             font-variant-numeric:tabular-nums;">'
        f'    {rcs:.1f} dB'
        f'  </td>'
        f'  <td style="padding:6px 10px;text-align:right;'
        f'             font-variant-numeric:tabular-nums;">'
        f'    {length:.0f} m'
        f'  </td>'
        f'  <td style="padding:6px 10px;text-align:right;'
        f'             font-variant-numeric:tabular-nums;">'
        f'    {conf:.2f}'
        f'  </td>'
        f'</tr>'
    )


def _build_dark_vessel_html(*, scene_id: str, scene_name: str,
                             scene_acquired_at: str,
                             n_dark_new: int, n_dark_continued: int,
                             top_detections: list[dict[str, Any]],
                             frontend_url: str) -> str:
    rows = "\n".join(_format_detection_row(d) for d in top_detections)
    extra = ""
    if len(top_detections) < n_dark_new:
        extra = (
            f'<tr><td colspan="4" style="padding:6px 10px;'
            f'        color:#7a8593;font-style:italic;">'
            f'… {n_dark_new - len(top_detections)} more on the map'
            f'</td></tr>'
        )

    deeplink = f"{frontend_url}/?scene={scene_id}"

    return f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#040810;color:#e6eaf2;
             font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
             font-size:14px;line-height:1.5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="max-width:640px;margin:0 auto;padding:24px;">
  <tr><td>
    <div style="font-family:ui-monospace,Menlo,monospace;
                font-size:11px;letter-spacing:0.12em;
                color:#5fd093;text-transform:uppercase;">
      Semper Safe · Dark vessel alert
    </div>
    <h1 style="margin:8px 0 16px;font-size:22px;color:#ffffff;">
      {n_dark_new} new dark vessel{'s' if n_dark_new != 1 else ''} in Texas-shoreline AOI
    </h1>
    <p style="color:#a5acba;margin:0 0 18px;">
      Sentinel-1 SAR detected {n_dark_new} non-cooperative target{'s' if n_dark_new != 1 else ''}
      with no AIS within the fusion window.
      {f' Plus {n_dark_continued} continuation{"s" if n_dark_continued != 1 else ""} of existing dark tracks.' if n_dark_continued else ''}
    </p>
    <div style="border:1px solid #1f242e;border-radius:6px;
                background:#0a0f1a;padding:14px;margin-bottom:16px;">
      <div style="font-size:11px;color:#7a8593;text-transform:uppercase;
                  letter-spacing:0.06em;margin-bottom:4px;">Source scene</div>
      <div style="font-family:ui-monospace,Menlo,monospace;font-size:13px;">
        {scene_name or scene_id}
      </div>
      <div style="font-size:12px;color:#a5acba;margin-top:4px;">
        Acquired {scene_acquired_at} UTC
      </div>
    </div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border:1px solid #1f242e;
                  border-radius:6px;overflow:hidden;background:#0a0f1a;
                  margin-bottom:18px;">
      <thead>
        <tr style="background:#11161f;">
          <th style="padding:8px 10px;text-align:left;font-size:11px;
                     color:#7a8593;text-transform:uppercase;letter-spacing:0.06em;">
            Position
          </th>
          <th style="padding:8px 10px;text-align:right;font-size:11px;
                     color:#7a8593;text-transform:uppercase;letter-spacing:0.06em;">
            RCS
          </th>
          <th style="padding:8px 10px;text-align:right;font-size:11px;
                     color:#7a8593;text-transform:uppercase;letter-spacing:0.06em;">
            Length
          </th>
          <th style="padding:8px 10px;text-align:right;font-size:11px;
                     color:#7a8593;text-transform:uppercase;letter-spacing:0.06em;">
            Conf
          </th>
        </tr>
      </thead>
      <tbody>
        {rows}
        {extra}
      </tbody>
    </table>
    <a href="{deeplink}"
       style="display:inline-block;padding:10px 18px;background:#5fd093;
              color:#040810;text-decoration:none;font-weight:600;
              border-radius:4px;letter-spacing:0.06em;">
      Open on the map →
    </a>
    <p style="color:#5a6172;font-size:12px;margin-top:24px;">
      You're receiving this because your address is in
      <code>ALERT_SUBSCRIBERS</code>. To stop, ask Luke to drop you from the list.
    </p>
  </td></tr>
</table>
</body></html>"""


def _build_dark_vessel_text(*, scene_id: str, n_dark_new: int,
                             scene_acquired_at: str,
                             top_detections: list[dict[str, Any]]) -> str:
    """Plain-text body so deliverability isn't HTML-only."""
    lines = [
        f"Semper Safe — Dark vessel alert",
        f"",
        f"{n_dark_new} new non-cooperative target{'s' if n_dark_new != 1 else ''} "
        f"in the Texas-shoreline AOI from a Sentinel-1 SAR pass acquired "
        f"{scene_acquired_at} UTC.",
        f"",
        f"Top detections:",
    ]
    for d in top_detections:
        lines.append(
            f"  {d['lat']:.4f}N {d['lon']:.4f}W  "
            f"rcs={d['rcs_db']:.1f}dB  L={d['length_m']:.0f}m  "
            f"conf={d['confidence']:.2f}"
        )
    if len(top_detections) < n_dark_new:
        lines.append(f"  ... {n_dark_new - len(top_detections)} more on the map")
    lines += ["", f"Scene: {scene_id}"]
    return "\n".join(lines)


# --- public alert paths --------------------------------------------------

def notify_dark_vessels(*, scene_id: str, scene_name: str,
                        scene_acquired_at: datetime,
                        n_dark_new: int, n_dark_continued: int,
                        sample: list[dict[str, Any]],
                        frontend_url: str = "https://sempersafe.live") -> dict:
    """Send a digest alert when fuse_detections finds new dark vessels.

    Caller passes:
      scene_id / scene_name / scene_acquired_at — populates the email
        header. acquired_at is the SAR-pass time, not the processing time.
      n_dark_new, n_dark_continued — counts from fuse_detections summary.
      sample — list of detection dicts (lat/lon/rcs_db/length_m/confidence)
        to render inline. Caller is responsible for filtering by
        confidence / sorting by RCS / capping at MAX_DETECTIONS_IN_BODY.
      frontend_url — base URL the "Open on the map" CTA points at.

    Returns:
      {"sent_to": [...], "skipped": "<reason>"} for the audit log.
    """
    if not is_configured():
        return {"skipped": "RESEND_API_KEY not set"}
    recips = subscribers()
    if not recips:
        return {"skipped": "ALERT_SUBSCRIBERS empty"}
    if n_dark_new <= 0:
        return {"skipped": "no new dark vessels"}

    top = sorted(
        [d for d in sample if (d.get("confidence") or 0) >= MIN_CONFIDENCE],
        key=lambda d: (-d.get("rcs_db", 0), -d.get("confidence", 0)),
    )[:MAX_DETECTIONS_IN_BODY]
    if not top:
        return {"skipped": f"no detections above conf {MIN_CONFIDENCE}"}

    acq = scene_acquired_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    subject = (
        f"{DEFAULT_SUBJECT_PREFIX} {n_dark_new} new dark vessel"
        f"{'s' if n_dark_new != 1 else ''} (Texas)"
    )
    html = _build_dark_vessel_html(
        scene_id=scene_id, scene_name=scene_name, scene_acquired_at=acq,
        n_dark_new=n_dark_new, n_dark_continued=n_dark_continued,
        top_detections=top, frontend_url=frontend_url.rstrip("/"),
    )
    text = _build_dark_vessel_text(
        scene_id=scene_id, n_dark_new=n_dark_new,
        scene_acquired_at=acq, top_detections=top,
    )

    try:
        resp = send_email(to=recips, subject=subject, html=html, text=text)
    except httpx.HTTPError as e:
        log.warning("alert send failed (HTTP): %s", e)
        return {"skipped": f"http error: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("alert send failed (unexpected)")
        return {"skipped": f"unexpected error: {e}"}

    log.info("alert sent to %d recipient(s); resend_id=%s",
             len(recips), resp.get("id"))
    return {"sent_to": recips, "resend_id": resp.get("id"),
            "subject": subject, "n_dark_new": n_dark_new}
