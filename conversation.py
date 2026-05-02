"""
Conversation manager — tracks state, turns, suppression, and merchant-level opt-outs.
"""
import time
import hashlib
from typing import Optional


class ConversationManager:
    def __init__(self):
        # conv_id -> {merchant_id, customer_id, trigger_id, suppression_key, turns, closed, opened_at}
        self._convs: dict[str, dict] = {}
        # suppression_key -> expiry_ts
        self._suppressed_triggers: dict[str, float] = {}
        # merchant_id -> expiry_ts
        self._suppressed_merchants: dict[str, float] = {}
        # (merchant_id, trigger_id) -> conv_id (for dedup)
        self._merchant_trigger_map: dict[tuple, str] = {}

    def open_conversation(self, conv_id: str, merchant_id: str, customer_id: Optional[str],
                          trigger_id: str, suppression_key: str):
        self._convs[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trigger_id,
            "suppression_key": suppression_key,
            "turns": [],
            "closed": False,
            "opened_at": time.time(),
        }
        self._merchant_trigger_map[(merchant_id, trigger_id)] = conv_id

    def add_turn(self, conv_id: str, role: str, body: str):
        if conv_id not in self._convs:
            self._convs[conv_id] = {"turns": [], "closed": False}
        self._convs[conv_id]["turns"].append({"role": role, "body": body})

    def get_history(self, conv_id: str) -> list[dict]:
        conv = self._convs.get(conv_id, {})
        return conv.get("turns", [])

    def get_conversation(self, conv_id: str) -> Optional[dict]:
        return self._convs.get(conv_id)

    def close_conversation(self, conv_id: str):
        if conv_id in self._convs:
            self._convs[conv_id]["closed"] = True

    def is_closed(self, conv_id: str) -> bool:
        conv = self._convs.get(conv_id, {})
        return conv.get("closed", False)

    def find_open_conversation(self, merchant_id: str, trigger_id: str) -> Optional[str]:
        conv_id = self._merchant_trigger_map.get((merchant_id, trigger_id))
        if conv_id and not self.is_closed(conv_id):
            return conv_id
        return None

    def suppress(self, trigger_id: str, seconds: float = 86400 * 7):
        """Suppress a trigger ID so it's not re-fired."""
        self._suppressed_triggers[trigger_id] = time.time() + seconds

    def is_suppressed(self, trigger_id: str) -> bool:
        expiry = self._suppressed_triggers.get(trigger_id)
        if expiry and time.time() < expiry:
            return True
        return False

    def suppress_merchant(self, merchant_id: Optional[str], days: int = 30):
        """Suppress all outreach to a merchant for N days."""
        if merchant_id:
            self._suppressed_merchants[merchant_id] = time.time() + days * 86400

    def is_merchant_suppressed(self, merchant_id: Optional[str]) -> bool:
        if not merchant_id:
            return False
        expiry = self._suppressed_merchants.get(merchant_id)
        return expiry and time.time() < expiry

    def is_repeat(self, conv_id: str, body: str) -> bool:
        """Check if this exact body was already sent in this conversation."""
        history = self.get_history(conv_id)
        h = _fingerprint(body)
        for turn in history:
            if turn.get("role") == "vera" and _fingerprint(turn.get("body", "")) == h:
                return True
        return False

    def clear(self):
        self._convs.clear()
        self._suppressed_triggers.clear()
        self._suppressed_merchants.clear()
        self._merchant_trigger_map.clear()


def _fingerprint(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()
