"""
Unit tests for hermes_store.update_room_registry.

Uses a minimal in-memory FakeDb that mimics the Firestore transactional API
just enough to exercise the promotion/eviction logic without hitting a real
Firebase project.

Run:
    cd orchestrator && python3 -m pytest tests/test_room_registry.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fake Firestore client ──────────────────────────────────────────────────

class _FakeSnap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def get(self, transaction=None):
        return _FakeSnap(self._store._docs.get(self._path))

    def set(self, data, merge=False):
        existing = self._store._docs.get(self._path, {})
        if merge:
            merged = dict(existing)
            merged.update(data)
            self._store._docs[self._path] = merged
        else:
            self._store._docs[self._path] = dict(data)


class _FakeCollection:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return _FakeDocRef(self._store, f"{self._name}/{doc_id}")


class _FakeTx:
    def __init__(self, store):
        self._store = store

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)


class FakeDb:
    def __init__(self):
        self._docs: dict[str, dict] = {}

    def collection(self, name):
        return _FakeCollection(self, name)

    def transaction(self):
        return _FakeTx(self)


# Patch firebase_admin.firestore.transactional to a no-op decorator
import types  # noqa: E402

_fake_module = types.SimpleNamespace(transactional=lambda fn: fn)
sys.modules.setdefault("firebase_admin", types.SimpleNamespace(firestore=_fake_module))
sys.modules.setdefault("firebase_admin.firestore", _fake_module)

import hermes_store as hs  # noqa: E402


def setup_function(fn):
    hs._db = FakeDb()
    hs._init_attempted = True
    hs.ROOM_REGISTRY_CAP = 5  # tighten for test


def _obs(kind, id, label=None, importance=50):
    return {"kind": kind, "id": id, "label": label or id, "importance": importance}


# ── Promotion threshold ────────────────────────────────────────────────────

def test_first_observation_is_pending():
    reg = hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087")])
    key = "sku:CAB-087"
    assert reg[key]["status"] == "pending"
    assert reg[key]["observationCount"] == 1


def test_second_observation_promotes_to_active():
    hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087")])
    reg = hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087")])
    key = "sku:CAB-087"
    assert reg[key]["status"] == "active"
    assert reg[key]["observationCount"] == 2


def test_importance_tracks_max_seen():
    hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087", importance=30)])
    hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087", importance=80)])
    reg = hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087", importance=50)])
    assert reg["sku:CAB-087"]["maxImportance"] == 80


# ── Pinning (admin seed) ───────────────────────────────────────────────────

def test_pin_marks_active_immediately():
    reg = hs.pin_room_entities("o1", "r1", [{"kind": "product_line", "id": "cabinet"}])
    key = "product_line:cabinet"
    assert reg[key]["pinned"] is True
    assert reg[key]["status"] == "active"


def test_pinned_survives_eviction_pressure():
    # Pin one
    hs.pin_room_entities("o1", "r1", [{"kind": "product_line", "id": "cabinet"}])
    # Add 10 different SKUs twice each (all promoted to active)
    for i in range(10):
        for _ in range(2):
            hs.update_room_registry("o1", "r1", [_obs("sku", f"X-{i:03d}", importance=100)])
    # Registry cap is 5 for active non-pinned; pinned should remain
    reg = hs._db._docs["hermesProfiles/room:o1:r1"]["entityRegistry"]
    assert reg.get("product_line:cabinet", {}).get("pinned") is True


# ── LFU eviction ───────────────────────────────────────────────────────────

def test_lfu_evicts_weakest_active():
    # 6 SKUs, each observed twice with varying importance
    # Weakest = lowest importance, should get evicted first
    for i, imp in enumerate([10, 20, 30, 40, 50, 60]):
        for _ in range(2):
            hs.update_room_registry("o1", "r1", [_obs("sku", f"X-{i}", importance=imp)])
    reg = hs._db._docs["hermesProfiles/room:o1:r1"]["entityRegistry"]
    active_keys = [k for k, v in reg.items() if v["status"] == "active"]
    assert len(active_keys) == 5  # cap
    assert "sku:X-0" not in active_keys  # weakest evicted


def test_pending_entities_never_evicted():
    # 10 SKUs all with 1 observation — all stay pending, none get evicted
    for i in range(10):
        hs.update_room_registry("o1", "r1", [_obs("sku", f"Y-{i}")])
    reg = hs._db._docs["hermesProfiles/room:o1:r1"]["entityRegistry"]
    pending = [k for k, v in reg.items() if v["status"] == "pending"]
    assert len(pending) == 10  # cap only applies to active


# ── Idempotence / timestamps ───────────────────────────────────────────────

def test_first_observed_at_preserved_across_updates():
    hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087")])
    first_snap = hs._db._docs["hermesProfiles/room:o1:r1"]["entityRegistry"]["sku:CAB-087"]["firstObservedAt"]
    hs.update_room_registry("o1", "r1", [_obs("sku", "CAB-087")])
    second_snap = hs._db._docs["hermesProfiles/room:o1:r1"]["entityRegistry"]["sku:CAB-087"]["firstObservedAt"]
    assert first_snap == second_snap


def test_multiple_entities_single_batch():
    reg = hs.update_room_registry("o1", "r1", [
        _obs("sku", "CAB-087"),
        _obs("product_line", "cabinet"),
        _obs("channel", "amazon"),
    ])
    assert len(reg) == 3
