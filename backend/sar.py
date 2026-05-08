"""
Sentinel-1 SAR ingestion (Phase 4 of docs/roadmap.md).

Pipeline stages:
  1. discover_scenes(bbox, since) — query Copernicus OData catalog for
     Sentinel-1 IW GRDH scenes intersecting the AOI. Public, no auth.
  2. record_scenes(...)            [Phase 4.1] insert into sar_scenes
  3. download_scene(...)           [Phase 4.2] requires Copernicus auth
  4. detect_cfar(...)              [Phase 4.3] NumPy CFAR per the
                                   roadmap's reference paper
  5. fuse_detections(...)          [Phase 4.4] hand each detection to
                                   maritime.ingest as a SourceType.SAR
                                   observation (engine handles match
                                   vs dark_vessel since Phase 1)

Catalog endpoint choice:
  Copernicus exposes both a STAC API (catalogue.dataspace.copernicus.eu/stac)
  and an OData API (.../odata/v1/). The STAC endpoint omits direct
  Sentinel collections — only Contributing Missions are listed there.
  OData is the canonical Sentinel-1 catalog and the only one that
  returns data for our query, so we use it.

This commit ships discover_scenes + record_scenes + footprint parsing.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

log = logging.getLogger("sar")


ODATA_PRODUCTS = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

# Texas-shoreline AOI per memory/semper_safe_aoi.md.
# OData wants a WKT polygon string.
TEXAS_SHORELINE_WKT = (
    "POLYGON((-98 25.5, -93.5 25.5, -93.5 30.5, -98 30.5, -98 25.5))"
)


# Copernicus emits Footprint as: geography'SRID=4326;POLYGON ((x y, x y, ...))'
# The POLYGON / MULTIPOLYGON portion is plain WKT — strip the prefix.
_FOOTPRINT_RE = re.compile(r"geography'SRID=\d+;\s*(.+?)'\s*$", re.DOTALL)


def _strip_footprint(footprint_str: str) -> str:
    m = _FOOTPRINT_RE.match(footprint_str.strip())
    return m.group(1).strip() if m else footprint_str.strip()


def discover_scenes(
    *,
    aoi_wkt: str = TEXAS_SHORELINE_WKT,
    since: datetime | None = None,
    until: datetime | None = None,
    sensor_mode: str = "IW",
    product_type_substr: str = "IW_GRDH",
    limit: int = 50,
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """Query Copernicus OData for Sentinel-1 scenes matching the filter.

    Returns list of normalized dicts (one per scene) with the fields
    we'd persist in sar_scenes:
      scene_id, name, platform, sensor_mode, polarization, acquired_at,
      footprint_wkt, source_url, content_length_bytes, online

    Defaults: last 14 days of IW GRDH (high-res ground-range-detected,
    the standard product type for vessel detection) over Texas shoreline.
    """
    until = until or datetime.now(timezone.utc)
    since = since or (until - timedelta(days=14))

    iso_since = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_until = until.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    odata_filter = (
        "Collection/Name eq 'SENTINEL-1'"
        f" and ContentDate/Start gt {iso_since}"
        f" and ContentDate/Start lt {iso_until}"
        f" and contains(Name, '{product_type_substr}')"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')"
    )
    params = {
        "$filter": odata_filter,
        "$top": str(limit),
        "$orderby": "ContentDate/Start desc",
    }

    log.info(
        "OData search: SENTINEL-1 %s since=%s until=%s limit=%d",
        product_type_substr, iso_since, iso_until, limit,
    )
    resp = httpx.get(ODATA_PRODUCTS, params=params, timeout=timeout_s)
    resp.raise_for_status()
    products = resp.json().get("value", [])
    log.info("OData returned %d products", len(products))

    out: list[dict[str, Any]] = []
    for p in products:
        name = p.get("Name", "")
        # Polarization + platform live in the product name. Defensive parse.
        platform = name[:3] if name.startswith("S1") else "S1?"
        # Polarization codes: 1SDV (dual VV+VH), 1SSV (single VV), etc.
        pol_match = re.search(r"_1S([A-Z]{2})_", name)
        pol_code = pol_match.group(1) if pol_match else "?"
        polarization = {
            "DV": "VV+VH", "DH": "HH+HV", "SV": "VV", "SH": "HH",
        }.get(pol_code, pol_code)

        out.append({
            "scene_id": p.get("Id"),
            "name": name,
            "platform": platform,
            "sensor_mode": sensor_mode,
            "polarization": polarization,
            "acquired_at": p.get("ContentDate", {}).get("Start"),
            "footprint_wkt": _strip_footprint(p.get("Footprint", "") or ""),
            "source_url": (
                f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({p['Id']})/$value"
            ),
            "content_length_bytes": int(p.get("ContentLength") or 0),
            "online": bool(p.get("Online")),
        })
    return out


def record_scenes(scenes: list[dict[str, Any]]) -> dict[str, int]:
    """Insert/upsert discovered scenes into sar_scenes with state='discovered'.

    Returns counts dict {inserted: N, skipped_existing: M}.
    Idempotent against re-running discover_scenes — existing scene_ids
    are left untouched.
    """
    if not scenes:
        return {"inserted": 0, "skipped_existing": 0}

    # Imported lazily so importing sar.py doesn't pull DB stack.
    from geoalchemy2.shape import from_shape
    from shapely import wkt as shapely_wkt
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from db import models as dbm
    from db.session import session_scope

    rows = []
    for s in scenes:
        try:
            geom = from_shape(shapely_wkt.loads(s["footprint_wkt"]), srid=4326)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping scene %s — bad footprint: %s",
                        s.get("scene_id"), exc)
            continue
        rows.append({
            "scene_id": s["scene_id"],
            "platform": s["platform"],
            "sensor_mode": s["sensor_mode"],
            "polarization": s["polarization"],
            "acquired_at": s["acquired_at"],
            "ingested_at": datetime.now(timezone.utc),
            "footprint": geom,
            "raw_url": None,
            "source_url": s["source_url"],
            "state": "discovered",
            "failure_reason": None,
            "attrs": {
                "content_length_bytes": s["content_length_bytes"],
                "online": s["online"],
                "name": s["name"],
            },
        })

    if not rows:
        return {"inserted": 0, "skipped_existing": 0}

    # psycopg3's rowcount on bulk INSERT-ON-CONFLICT is unreliable
    # (returns -1). Diff against the existing scene_id set instead.
    from sqlalchemy import select as sa_select

    candidate_ids = [r["scene_id"] for r in rows]
    with session_scope() as s:
        existing = set(s.execute(
            sa_select(dbm.SarSceneRow.scene_id).where(
                dbm.SarSceneRow.scene_id.in_(candidate_ids),
            )
        ).scalars())
        new_rows = [r for r in rows if r["scene_id"] not in existing]
        if new_rows:
            s.execute(
                pg_insert(dbm.SarSceneRow)
                .values(new_rows)
                .on_conflict_do_nothing(index_elements=["scene_id"])
            )

    return {"inserted": len(new_rows), "skipped_existing": len(rows) - len(new_rows)}


def _gen_detection_id() -> str:
    return f"sard_{uuid.uuid4().hex[:12]}"


# --- Copernicus auth -------------------------------------------------

CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)


class CopernicusAuth:
    """Simple OAuth client for Copernicus Data Space.

    Caches an access_token in memory, refreshes via refresh_token
    before expiry. Falls back to password grant when refresh fails.

    Reads credentials from env vars CDSE_USERNAME and CDSE_PASSWORD
    (set as Fly secrets). Returns None from token() if either is unset
    so callers can short-circuit.
    """

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._access_expires_at: float = 0.0
        self._refresh_token: str | None = None
        self._refresh_expires_at: float = 0.0

    @staticmethod
    def is_configured() -> bool:
        import os
        return bool(os.environ.get("CDSE_USERNAME")) and bool(os.environ.get("CDSE_PASSWORD"))

    def token(self, *, leeway_s: int = 30) -> str | None:
        """Return a valid access_token. Re-fetch via refresh or password
        grant as needed. Returns None if unconfigured."""
        import os
        import time

        if not self.is_configured():
            return None

        now = time.time()
        if self._access_token and now < (self._access_expires_at - leeway_s):
            return self._access_token

        # Try refresh first if we have one with time left.
        if self._refresh_token and now < (self._refresh_expires_at - leeway_s):
            data = self._post_token({
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": "cdse-public",
            })
            if data:
                self._consume(data, now)
                return self._access_token

        # Fallback: full password grant.
        data = self._post_token({
            "grant_type": "password",
            "username": os.environ["CDSE_USERNAME"],
            "password": os.environ["CDSE_PASSWORD"],
            "client_id": "cdse-public",
        })
        if not data:
            log.warning("Copernicus password grant failed")
            return None
        self._consume(data, now)
        return self._access_token

    def _post_token(self, form: dict[str, str]) -> dict | None:
        try:
            r = httpx.post(CDSE_TOKEN_URL, data=form, timeout=20)
        except httpx.HTTPError as e:
            log.warning("Copernicus token request failed: %s", e)
            return None
        if r.status_code != 200:
            log.warning("Copernicus token %d: %s", r.status_code, r.text[:200])
            return None
        return r.json()

    def _consume(self, data: dict, now: float) -> None:
        self._access_token = data["access_token"]
        self._access_expires_at = now + int(data.get("expires_in", 1800))
        self._refresh_token = data.get("refresh_token")
        self._refresh_expires_at = now + int(data.get("refresh_expires_in", 3600))


_auth_singleton: CopernicusAuth | None = None


def auth() -> CopernicusAuth:
    """Process-wide CopernicusAuth singleton. Caches the token in memory."""
    global _auth_singleton
    if _auth_singleton is None:
        _auth_singleton = CopernicusAuth()
    return _auth_singleton


# --- Download --------------------------------------------------------

PRODUCT_DOWNLOAD_URL_TPL = (
    "https://zipper.dataspace.copernicus.eu/odata/v1/Products({scene_id})/$value"
)


def download_scene_to_r2(scene_id: str, *,
                          http_chunk_size: int = 1 << 20,        # 1 MB read from httpx
                          part_size: int = 16 * 1024 * 1024,     # 16 MB S3 part
                          timeout_s: float = 600.0) -> dict:
    """True-stream Sentinel-1 product → Cloudflare R2 via S3 multipart upload.

    Memory profile (vs the old SpooledTemporaryFile path that fully
    materialized the scene before uploading):
      - httpx read buffer:      http_chunk_size  (1 MB)
      - accumulating part buf:  ≤ part_size      (16 MB)
      - upload_part body:       ≤ part_size      (16 MB, in flight)

    Total peak ≈ 32-48 MB regardless of scene size. Critical on the
    512 MB Fly VM: with AIS ingest, audit archive, gap sweeper, DB pool,
    and CFAR/numpy in the same process, we can't afford to spool 1-2 GB
    of GRDH product to disk *or* RAM.

    S3 multipart constraints:
      - parts must be ≥ 5 MB except the last
      - max 10000 parts → at 16 MB/part we cap at ~160 GB scenes (fine)
      - ETags and part numbers must be in order at complete-time

    Updates sar_scenes:
      raw_url    = r2://bucket/key on success
      state      = 'downloaded' / 'failed'
      failure_reason populated on error

    Aborts the multipart upload on any exception so we don't leak
    half-uploaded objects (R2 bills you for incomplete multipart parts).
    """
    from db import models as dbm
    from db import archive
    from db.session import session_scope

    if not auth().is_configured():
        raise RuntimeError("Copernicus auth not configured")
    cfg = archive._r2_config()  # noqa: SLF001 — reuse the validated R2 config
    if cfg is None:
        raise RuntimeError("R2 not configured (R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/...)")

    tok = auth().token()
    if not tok:
        raise RuntimeError("Copernicus token fetch failed")

    url = PRODUCT_DOWNLOAD_URL_TPL.format(scene_id=scene_id)
    key = f"sar/scenes/{scene_id}.SAFE.zip"
    raw_url = f"r2://{cfg['bucket']}/{key}"

    with session_scope() as s:
        scene = s.get(dbm.SarSceneRow, scene_id)
        if scene is None:
            raise RuntimeError(f"sar_scenes row not found: {scene_id}")
        if scene.state == "downloaded" and scene.raw_url:
            return {"scene_id": scene_id, "skipped": "already downloaded",
                    "raw_url": scene.raw_url}

    if part_size < 5 * 1024 * 1024:
        raise ValueError("S3 part_size must be ≥ 5 MB")

    client = archive._r2_client(cfg)  # noqa: SLF001
    bucket = cfg["bucket"]

    log.info("starting multipart upload for scene %s → %s", scene_id, raw_url)
    create = client.create_multipart_upload(
        Bucket=bucket, Key=key, ContentType="application/zip",
    )
    upload_id = create["UploadId"]

    parts: list[dict] = []
    bytes_seen = 0
    part_buf = bytearray()
    part_number = 1

    def _flush_part(buf: bytearray, n: int) -> None:
        # Hand the bytes to upload_part; reset caller's buffer afterwards.
        if not buf:
            return
        resp = client.upload_part(
            Bucket=bucket, Key=key, UploadId=upload_id,
            PartNumber=n, Body=bytes(buf),
        )
        parts.append({"PartNumber": n, "ETag": resp["ETag"]})
        log.info("uploaded part %d (%d bytes) for scene %s",
                 n, len(buf), scene_id)

    try:
        with httpx.stream(
            "GET", url, headers={"Authorization": f"Bearer {tok}"},
            timeout=timeout_s, follow_redirects=True,
        ) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(http_chunk_size):
                if not chunk:
                    continue
                part_buf.extend(chunk)
                bytes_seen += len(chunk)
                while len(part_buf) >= part_size:
                    head = part_buf[:part_size]
                    del part_buf[:part_size]
                    _flush_part(head, part_number)
                    part_number += 1
        # Final part (any size, including > 0 and < part_size; S3 allows
        # the last part to be < 5 MB).
        if part_buf:
            _flush_part(part_buf, part_number)
            part_number += 1

        if not parts:
            # Edge case: zero-byte response. Abort and bail.
            raise RuntimeError("download produced 0 bytes")

        client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception as exc:  # noqa: BLE001
        try:
            client.abort_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
            )
        except Exception as abort_exc:  # noqa: BLE001
            log.warning("abort_multipart_upload also failed for %s: %s",
                        scene_id, abort_exc)
        with session_scope() as s:
            scene = s.get(dbm.SarSceneRow, scene_id)
            if scene is not None:
                scene.state = "failed"
                scene.failure_reason = f"{type(exc).__name__}: {exc}"[:500]
        log.exception("download_scene_to_r2 failed for %s", scene_id)
        raise

    with session_scope() as s:
        scene = s.get(dbm.SarSceneRow, scene_id)
        if scene is not None:
            scene.raw_url = raw_url
            scene.state = "downloaded"
            scene.failure_reason = None
            scene.attrs = {**(scene.attrs or {}), "bytes_uploaded": bytes_seen,
                           "parts": len(parts)}

    log.info("downloaded scene %s → %s (%d bytes, %d parts)",
             scene_id, raw_url, bytes_seen, len(parts))
    return {
        "scene_id": scene_id,
        "raw_url": raw_url,
        "bytes": bytes_seen,
        "parts": len(parts),
        "key": key,
    }


def download_scene(scene_id: str, *, dest_path: str,
                   chunk_size: int = 1 << 20,
                   timeout_s: float = 600.0) -> dict:
    """Stream-download a Sentinel-1 product .SAFE.zip to a local path.

    Sized for the Fly free tier: chunked HTTP streaming so memory stays
    O(chunk_size) ≈ 1 MB regardless of the 1-2 GB scene size.

    Returns metadata dict {scene_id, dest_path, bytes, content_type}.

    Note: this writes to local disk. Phase 4.2-next swaps the local
    write for a multipart upload to R2 so we don't need a Fly volume.
    """
    tok = auth().token()
    if not tok:
        raise RuntimeError("Copernicus auth not configured (CDSE_USERNAME/CDSE_PASSWORD)")

    url = PRODUCT_DOWNLOAD_URL_TPL.format(scene_id=scene_id)
    headers = {"Authorization": f"Bearer {tok}"}
    total_bytes = 0
    content_type = None

    with httpx.stream("GET", url, headers=headers,
                      timeout=timeout_s, follow_redirects=True) as r:
        r.raise_for_status()
        content_type = r.headers.get("Content-Type")
        with open(dest_path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size):
                f.write(chunk)
                total_bytes += len(chunk)

    log.info("downloaded scene %s → %s (%d bytes)", scene_id, dest_path, total_bytes)
    return {
        "scene_id": scene_id,
        "dest_path": dest_path,
        "bytes": total_bytes,
        "content_type": content_type,
    }

