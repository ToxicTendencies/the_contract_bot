"""In-memory contract state with TTL cleanup.

Nothing here survives a restart on purpose: contracts are ephemeral, and the
only persistent artefact is the archive of finished images in the owner's chat.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

STATE_PREVIEW = "preview"
STATE_OPEN = "open"
STATE_SIGNED = "signed"
STATE_CLOSED = "closed"


@dataclass(frozen=True)
class Signature:
    field_key: str
    text: str
    user_id: int
    user_name: str
    at: float


@dataclass
class PendingSignature:
    field_key: str
    user_id: int
    deadline: float

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.deadline


@dataclass
class Contract:
    token: str
    chat_key: str
    template_key: str
    inline_message_id: str | None
    initiator_id: int
    initiator_name: str = ""
    state: str = STATE_PREVIEW
    page: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    signatures: dict[str, Signature] = field(default_factory=dict)
    pending: PendingSignature | None = None

    def touch(self) -> None:
        self.updated_at = time.time()

    @property
    def is_complete(self) -> bool:
        return self.state == STATE_SIGNED

    def same_signers(self) -> bool:
        users = {signature.user_id for signature in self.signatures.values()}
        return len(users) == 1 and len(self.signatures) > 1


class ContractStore:
    """Dicts in RAM, guarded by TTL and a hard cap."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        pending_seconds: int,
        max_active: int,
    ):
        self.ttl_seconds = ttl_seconds
        self.pending_seconds = pending_seconds
        self.max_active = max_active
        self._by_token: dict[str, Contract] = {}
        self._by_chat: dict[str, str] = {}

    # -- lookups ---------------------------------------------------------

    def get(self, token: str | None) -> Contract | None:
        if not token:
            return None
        return self._by_token.get(token)

    def for_chat(self, chat_key: str) -> Contract | None:
        token = self._by_chat.get(chat_key)
        return self._by_token.get(token) if token else None

    @property
    def active_count(self) -> int:
        return len(self._by_token)

    @property
    def pending_count(self) -> int:
        return sum(1 for contract in self._by_token.values() if contract.pending is not None)

    # -- mutations -------------------------------------------------------

    def create(
        self,
        *,
        chat_key: str,
        template_key: str,
        initiator_id: int,
        initiator_name: str = "",
        page: int = 0,
    ) -> Contract:
        previous = self.for_chat(chat_key)
        if previous is not None:
            self.drop(previous)

        token = secrets.token_urlsafe(6)[:8]
        while token in self._by_token:
            token = secrets.token_urlsafe(6)[:8]

        contract = Contract(
            token=token,
            chat_key=chat_key,
            template_key=template_key,
            inline_message_id=None,
            initiator_id=initiator_id,
            initiator_name=initiator_name,
            page=page,
        )
        self._by_token[token] = contract
        self._by_chat[chat_key] = token
        self._enforce_cap()
        return contract

    def drop(self, contract: Contract) -> None:
        self._by_token.pop(contract.token, None)
        if self._by_chat.get(contract.chat_key) == contract.token:
            self._by_chat.pop(contract.chat_key, None)

    def drop_token(self, token: str | None) -> None:
        contract = self.get(token)
        if contract:
            self.drop(contract)

    def set_pending(self, contract: Contract, field_key: str, user_id: int) -> None:
        contract.pending = PendingSignature(
            field_key=field_key,
            user_id=user_id,
            deadline=time.time() + self.pending_seconds,
        )
        contract.touch()

    def clear_pending(self, contract: Contract) -> None:
        contract.pending = None
        contract.touch()

    def add_signature(
        self,
        contract: Contract,
        *,
        field_key: str,
        text: str,
        user_id: int,
        user_name: str,
    ) -> Signature:
        signature = Signature(
            field_key=field_key,
            text=text,
            user_id=user_id,
            user_name=user_name,
            at=time.time(),
        )
        contract.signatures[field_key] = signature
        contract.pending = None
        contract.touch()
        return signature

    # -- housekeeping ----------------------------------------------------

    def _enforce_cap(self) -> None:
        if len(self._by_token) <= self.max_active:
            return
        ordered = sorted(self._by_token.values(), key=lambda item: item.updated_at)
        for contract in ordered[: len(self._by_token) - self.max_active]:
            self.drop(contract)

    def expire_pending(self, now: float | None = None) -> list[Contract]:
        now = now or time.time()
        expired: list[Contract] = []
        for contract in list(self._by_token.values()):
            if contract.pending and contract.pending.expired(now):
                contract.pending = None
                expired.append(contract)
        return expired

    def pop_stale(self, now: float | None = None) -> list[Contract]:
        """Remove and return contracts that sat idle for longer than TTL."""
        now = now or time.time()
        stale: list[Contract] = []
        for contract in list(self._by_token.values()):
            if now - contract.updated_at > self.ttl_seconds:
                stale.append(contract)
                self.drop(contract)
        return stale

    def clear(self) -> None:
        self._by_token.clear()
        self._by_chat.clear()


def chat_key(chat_id: int, thread_id: int | None = None, *, guest: bool = True) -> str:
    prefix = "g" if guest else "c"
    if thread_id:
        return f"{prefix}{chat_id}:{thread_id}"
    return f"{prefix}{chat_id}"
