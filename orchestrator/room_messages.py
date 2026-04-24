"""
Shared helper for the orchestrator to post synthetic assistant messages into
a hive room's chat — used by both the Phase 6d approval flow and the Phase 7
task worker.

Why transactional: both the Next.js frontend (saveRoomMessages) and this
helper append to `chatRooms/{roomId}/messages`, indexed by a monotonic
`index` field. If we read the current max index then write separately, two
concurrent appenders (chat stream + task result landing at the same moment)
can pick the SAME next_index and produce a duplicate — the frontend ordered
listener then renders the messages out of order. Txn: read room.messageCount,
use that as the next index, write the message + bump the counter in one
atomic operation.

Counter semantics: both the frontend's saveRoomMessages and the former
post_room_approval_message used Increment(n) on room.messageCount. This
helper reads the current count as the next index, then writes count+1 back.
"""
from __future__ import annotations

import logging
from typing import Optional

from hermes_store import _get_db, _now_iso  # reuse existing ISO formatter

logger = logging.getLogger("room_messages")


def post_synthetic_message(
    *,
    room_id: str,
    text: str,
    role: str = "assistant",
    meta: dict | None = None,
    sender_user_id: str | None = None,
    sender_display_name: str | None = None,
    attachments: list[dict] | None = None,
) -> Optional[str]:
    """Append a message to chatRooms/{roomId}/messages in a single
    transaction that allocates `index` from room.messageCount. Returns
    the new message doc id, or None on failure.

    Shape matches Next.js saveRoomMessages so the chat UI renders it the
    same way as any normal turn: clientId=None, role, parts=[{type:text,text}],
    content=text, createdAt, index. Optional sender fields are included for
    user-role messages so the UI can attribute the message properly
    (e.g. "Schedule · Daily audit" for scheduled-run user turns).
    """
    if not room_id or not text:
        return None
    db = _get_db()
    if db is None:
        return None

    from firebase_admin import firestore as _fs

    room_ref = db.collection("chatRooms").document(room_id)
    messages_ref = room_ref.collection("messages")
    msg_ref = messages_ref.document()  # auto-generated id, known before txn
    new_msg_id = msg_ref.id
    now_iso = _now_iso()

    try:
        transaction = db.transaction()

        @_fs.transactional
        def _txn(txn):
            room_snap = room_ref.get(transaction=txn)
            if not room_snap.exists:
                logger.warning("post_synthetic_message: room %s not found", room_id)
                raise RuntimeError("room_not_found")
            room_data = room_snap.to_dict() or {}
            # Authoritative next_index — read MAX(index) from the messages
            # collection itself, same as saveRoomMessages on the frontend.
            # Don't trust `room.messageCount` alone: it drifts from the real
            # max whenever a writer skips the increment (e.g. frontend's
            # FieldValue.increment was undefined in some import path) or
            # whenever two writers race outside this txn. Drift produced
            # synthetic approval messages with index=0 → they floated to
            # the top of the chat above current turns.
            #
            # Pattern: take MAX(actual messages index, room.messageCount).
            # Within the txn the read set includes the messages query, so
            # a concurrent batch on messages forces our commit to retry.
            raw_count = room_data.get("messageCount")
            stored_count = int(raw_count) if raw_count is not None else 0
            actual_max = -1
            max_query = (
                messages_ref.order_by("index", direction=_fs.Query.DESCENDING)
                .limit(1)
            )

            def _extract_max(snap_list):
                if not snap_list:
                    return -1
                raw = snap_list[0].to_dict().get("index")
                return int(raw) if raw is not None else -1

            try:
                actual_max = _extract_max(list(txn.get(max_query)))
            except Exception as exc:
                logger.warning("txn.get(max_query) failed (room=%s): %s", room_id, exc)
            if actual_max < 0:
                try:
                    actual_max = _extract_max(list(max_query.get()))
                except Exception as exc:
                    logger.error(
                        "post_synthetic_message: BOTH max-index lookups failed (room=%s): %s",
                        room_id, exc,
                    )
            next_index = max(stored_count, actual_max + 1)
            logger.info(
                "post_synthetic_message: room=%s stored_count=%d actual_max=%d → next_index=%d",
                room_id, stored_count, actual_max, next_index,
            )

            parts: list[dict] = [{"type": "text", "text": text}]
            if attachments:
                for att in attachments:
                    parts.append({
                        "type": "data-attachment",
                        "data": {
                            "attachmentId": att["attachmentId"],
                            "name": att["name"],
                            "mimeType": att["mimeType"],
                            "sizeBytes": att.get("sizeBytes", 0),
                        },
                    })
            msg_doc: dict = {
                "clientId": None,
                "role": role,
                "parts": parts,
                "content": text,
                "createdAt": now_iso,
                "index": next_index,
            }
            if meta:
                msg_doc["meta"] = meta
            if sender_user_id:
                msg_doc["senderUserId"] = sender_user_id
            if sender_display_name:
                msg_doc["senderDisplayName"] = sender_display_name

            txn.set(msg_ref, msg_doc)
            # Heal stored_count back up if it had drifted below actual max
            # — avoids the next writer hitting the same off-by-N issue.
            txn.update(room_ref, {
                "messageCount": next_index + 1,
                "lastMessageAt": now_iso,
                "updatedAt": now_iso,
            })

        _txn(transaction)
    except RuntimeError:
        return None
    except Exception as exc:
        logger.warning(
            "post_synthetic_message txn failed (room=%s role=%s): %s",
            room_id, role, exc,
        )
        return None

    return new_msg_id


# Backward-compat alias. Phase 6d callers (pending_actions.py) pass only
# room_id/text/meta and always want an assistant role — keep the existing
# name working so the confirmation-outcome chat messages don't need to
# change.
def post_synthetic_assistant_message(
    *,
    room_id: str,
    text: str,
    meta: dict | None = None,
) -> Optional[str]:
    return post_synthetic_message(
        room_id=room_id, text=text, role="assistant", meta=meta,
    )
