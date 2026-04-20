"""
Phase 10 — attachment resolution for the orchestrator.

The Next.js frontend uploads files directly to GCS and writes an
`attachments/{id}` metadata doc. User chat messages carry a reference via
`data-attachment` parts with shape:

    {"type": "data-attachment",
     "data": {"attachmentId": "...", "name": "...", "mimeType": "...", "sizeBytes": N}}

This module walks those parts, fetches the authoritative Firestore doc, mints
a fresh 7-day signed download URL, and returns a normalized payload the
executor injects as `LYNX_ATTACHMENTS_JSON`.

Security model (matches the Next.js download route):
  The Firestore doc is the authority. We verify org + room match the caller's
  request context before surfacing a URL — prevents a model-generated
  attachmentId in the wrong scope from leaking a file.
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional

from hermes_store import _get_db

logger = logging.getLogger("attachments")

HIVE_BUCKET_NAME = "zeonsolutions"
SIGNED_URL_TTL = datetime.timedelta(days=7)  # GCS v4 max

_storage_client = None


def _get_storage_client():
    """Lazy-init a google-cloud-storage client using the same service account
    that powers firebase_admin. Reuses the firestore-credentials.json file
    already on disk; no new secret to manage."""
    global _storage_client
    if _storage_client is not None:
        return _storage_client
    try:
        from google.cloud import storage  # requires google-cloud-storage pkg
        from google.oauth2 import service_account
    except Exception as exc:
        logger.warning("google-cloud-storage not installed: %s", exc)
        return None

    # Firebase admin finds its creds via multiple paths; mirror that lookup.
    candidates = [
        Path(__file__).parent / "firestore-credentials.json",
        Path(__file__).parent.parent / "firestore-credentials.json",
    ]
    cred_path = next((p for p in candidates if p.exists()), None)
    if cred_path is None:
        logger.warning("No firestore-credentials.json found; storage disabled")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(str(cred_path))
        _storage_client = storage.Client(credentials=creds, project="zeon-solutions")
        logger.info("GCS client initialized (bucket=%s)", HIVE_BUCKET_NAME)
    except Exception as exc:
        logger.error("Failed to init GCS client: %s", exc)
        _storage_client = None
    return _storage_client


def extract_attachment_ids_from_message(message: dict | object) -> list[str]:
    """Walk a UIMessage-like value (Pydantic model OR dict) and return the
    list of attachmentIds referenced by `data-attachment` parts.
    """
    parts = None
    if hasattr(message, "parts"):
        parts = getattr(message, "parts") or []
    elif isinstance(message, dict):
        parts = message.get("parts") or []
    if not parts:
        return []

    ids: list[str] = []
    for p in parts:
        ptype = getattr(p, "type", None) if hasattr(p, "type") else (
            p.get("type") if isinstance(p, dict) else None
        )
        if ptype != "data-attachment":
            continue
        data = getattr(p, "data", None) if hasattr(p, "data") else (
            p.get("data") if isinstance(p, dict) else None
        )
        if not data:
            continue
        aid = data.get("attachmentId") if isinstance(data, dict) else None
        if aid and isinstance(aid, str):
            ids.append(aid)
    return ids


def resolve_attachments_for_skill(
    *,
    attachment_ids: list[str],
    expected_org_id: str,
    expected_room_id: Optional[str],
) -> list[dict]:
    """Fetch each attachment doc, validate scope, mint a signed URL, and
    return the payload shape the skill consumes via LYNX_ATTACHMENTS_JSON.

    Attachments whose orgId/roomId don't match the expected scope are
    silently skipped — prevents cross-room leakage if a client or model
    slipped a rogue attachmentId into the message.
    """
    logger.info(
        "[phase10] resolve_attachments start ids=%d org=%s room=%s",
        len(attachment_ids), expected_org_id, expected_room_id,
    )
    if not attachment_ids:
        return []
    db = _get_db()
    if db is None:
        logger.warning("[phase10] Firestore unavailable; cannot resolve attachments")
        return []
    storage_client = _get_storage_client()
    if storage_client is None:
        logger.warning("[phase10] GCS client unavailable; attachments skipped")
        return []

    bucket = storage_client.bucket(HIVE_BUCKET_NAME)
    out: list[dict] = []
    for aid in attachment_ids:
        try:
            snap = db.collection("attachments").document(aid).get()
        except Exception as exc:
            logger.warning("[phase10] Failed to read attachment %s: %s", aid, exc)
            continue
        if not snap.exists:
            logger.warning("[phase10] Attachment %s not found in Firestore", aid)
            continue
        doc = snap.to_dict() or {}

        # Scope check — the Firestore doc is the authority. If the model
        # somehow referenced an attachment from a different org/room, refuse.
        if doc.get("orgId") != expected_org_id:
            logger.warning(
                "[phase10] Attachment %s orgId mismatch (doc=%s expected=%s) — refusing",
                aid, doc.get("orgId"), expected_org_id,
            )
            continue
        if expected_room_id and doc.get("roomId") != expected_room_id:
            logger.warning(
                "[phase10] Attachment %s roomId mismatch (doc=%s expected=%s) — refusing",
                aid, doc.get("roomId"), expected_room_id,
            )
            continue

        storage_path = doc.get("storagePath")
        if not storage_path:
            logger.warning("[phase10] Attachment %s has no storagePath", aid)
            continue

        try:
            blob = bucket.blob(storage_path)
            url = blob.generate_signed_url(
                version="v4",
                expiration=SIGNED_URL_TTL,
                method="GET",
            )
        except Exception as exc:
            logger.warning(
                "[phase10] Failed to mint signed URL for %s (path=%s): %s",
                aid, storage_path, exc,
            )
            continue

        logger.info(
            "[phase10] resolved attachment id=%s name=%s mime=%s size=%dB",
            aid, doc.get("name"), doc.get("mimeType"), int(doc.get("sizeBytes") or 0),
        )
        out.append({
            "id": aid,
            "name": doc.get("name") or aid,
            "mimeType": doc.get("mimeType") or "application/octet-stream",
            "url": url,
            "sizeBytes": int(doc.get("sizeBytes") or 0),
        })
    logger.info(
        "[phase10] resolve_attachments done resolved=%d/%d",
        len(out), len(attachment_ids),
    )
    return out
