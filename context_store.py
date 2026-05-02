"""
In-memory context store with versioning.
Handles categories, merchants, customers, and triggers.
"""
from typing import Any, Optional

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


class ContextStore:
    def __init__(self):
        # (scope, context_id) -> {"version": int, "payload": dict}
        self._store: dict[tuple[str, str], dict] = {}

    def upsert(self, scope: str, context_id: str, version: int, payload: dict) -> str:
        if scope not in VALID_SCOPES:
            return "invalid_scope"
        key = (scope, context_id)
        existing = self._store.get(key)
        if existing and existing["version"] >= version:
            return "stale"
        self._store[key] = {"version": version, "payload": payload}
        return "ok"

    def get_payload(self, scope: str, context_id: str) -> Optional[dict]:
        if not context_id:
            return None
        entry = self._store.get((scope, context_id))
        return entry["payload"] if entry else None

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        entry = self._store.get((scope, context_id))
        return entry["version"] if entry else None

    def get_counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _) in self._store:
            if scope in counts:
                counts[scope] += 1
        return counts

    def clear(self):
        self._store.clear()

    def all_triggers(self) -> list[dict]:
        result = []
        for (scope, cid), entry in self._store.items():
            if scope == "trigger":
                result.append({"id": cid, **entry["payload"]})
        return result

    def all_merchants(self) -> list[dict]:
        result = []
        for (scope, cid), entry in self._store.items():
            if scope == "merchant":
                result.append(entry["payload"])
        return result
