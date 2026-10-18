"""In-memory contract state with TTL cleanup.

Nothing here survives a restart on purpose: contracts are ephemeral, and the
only persistent artefact is the archive of finished images in the owner's chat.

Two entry points share the same Contract:

* inline mode — contract is bound to ``inline_message_id``; signatures are
  collected in the bot's DM after the user presses a button;
* guest mode — contract is bound to a chat key; signatures are collected by
  replying to the bot's message in that chat.

Pending input is tracked per user, so two people can be typing their
signatures at the same time.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

STATE_PREVIEW = "preview"
STATE_OPEN = "open"
STATE_SIGNED = "signed"
STATE_CLOSED = "closed"

TOKEN_LENGTH = 8


@dataclass(frozen=True)
class Signature:
    field_key: str
    text: str
    user_id: int
    user_name: str = ""
    user_username: str = ""
    at: float = 0.0


@dataclass
class PendingSignature:
    token: str
    field_key: str
    user_id: int
    deadline: float

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.deadline


@dataclass
class Contract:
    token: str
    template_key: str
    chat_key: str | None = None
    inline_message_id: str | None = None
    chat_id: int | None = None
    message_id: int | None = None
    initiator_id: int = 0
    initiator_name: str = ""
    state: str = STATE_PREVIEW
    page: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    signatures: dict[str, Signature] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = time.time()

    @property
    def is_complete(self) -> bool:
        return self.state == STATE_SIGNED

    def same_signers(self) -> bool:
        users = {signature.user_id for signature in self.signatures.values()}
        return len(users) == 1 and len(self.signatures) > 1


def new_token() -> str:
    """Hex token: no ``_`` or ``-`` so it can live inside deep-link payloads."""
    return secrets.token_hex(TOKEN_LENGTH // 2)


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
        # В чате может жить несколько контрактов: у каждого сообщения свой
        # токен, иначе кнопки в предыдущем сообщении умирают, как только
        # кто-то вызвал бота ещё раз.
        self.max_per_chat = 5
        self._by_token: dict[str, Contract] = {}
        self._chat_tokens: dict[str, list[str]] = {}
        self._by_inline: dict[str, str] = {}
        self._pending: dict[int, PendingSignature] = {}

    # -- lookups ---------------------------------------------------------

    def get(self, token: str | None) -> Contract | None:
        if not token:
            return None
        return self._by_token.get(token)

    def for_chat(self, chat_key: str) -> Contract | None:
        """Самый свежий живой контракт чата."""
        for token in reversed(self._chat_tokens.get(chat_key, [])):
            contract = self._by_token.get(token)
            if contract is not None:
                return contract
        return None

    def for_inline(self, inline_message_id: str | None) -> Contract | None:
        if not inline_message_id:
            return None
        token = self._by_inline.get(inline_message_id)
        return self._by_token.get(token) if token else None

    def pending_for(self, user_id: int) -> PendingSignature | None:
        pending = self._pending.get(user_id)
        if pending is None:
            return None
        if pending.expired():
            self._pending.pop(user_id, None)
            return None
        return pending

    def pending_holder(self, token: str, field_key: str) -> int | None:
        """Who is currently typing a signature for this field, if anyone."""
        now = time.time()
        for user_id, pending in list(self._pending.items()):
            if pending.expired(now):
                self._pending.pop(user_id, None)
                continue
            if pending.token == token and pending.field_key == field_key:
                return user_id
        return None

    @property
    def active_count(self) -> int:
        return len(self._by_token)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # -- mutations -------------------------------------------------------

    def create(
        self,
        *,
        template_key: str,
        chat_key: str | None = None,
        inline_message_id: str | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        initiator_id: int = 0,
        initiator_name: str = "",
        page: int = 0,
        token: str | None = None,
    ) -> Contract:
        token = token or new_token()
        while token in self._by_token:
            token = new_token()

        contract = Contract(
            token=token,
            template_key=template_key,
            chat_key=chat_key,
            inline_message_id=inline_message_id or None,
            chat_id=chat_id,
            message_id=message_id,
            initiator_id=initiator_id,
            initiator_name=initiator_name,
            page=page,
        )
        self._by_token[token] = contract
        if chat_key:
            self._chat_tokens.setdefault(chat_key, []).append(token)
            self._trim_chat(chat_key)
        if inline_message_id:
            self._by_inline[inline_message_id] = token
        self._enforce_cap()
        return contract

    def _trim_chat(self, chat_key: str) -> None:
        tokens = self._chat_tokens.get(chat_key, [])
        while len(tokens) > self.max_per_chat:
            oldest = tokens.pop(0)
            contract = self._by_token.get(oldest)
            if contract is not None:
                self.drop(contract)

    def bind_inline(self, contract: Contract, inline_message_id: str) -> None:
        contract.inline_message_id = inline_message_id
        self._by_inline[inline_message_id] = contract.token
        contract.touch()

    def drop(self, contract: Contract) -> None:
        self._by_token.pop(contract.token, None)
        if contract.chat_key:
            tokens = self._chat_tokens.get(contract.chat_key)
            if tokens and contract.token in tokens:
                tokens.remove(contract.token)
            if not tokens:
                self._chat_tokens.pop(contract.chat_key, None)
        if (
            contract.inline_message_id
            and self._by_inline.get(contract.inline_message_id) == contract.token
        ):
            self._by_inline.pop(contract.inline_message_id, None)
        for user_id, pending in list(self._pending.items()):
            if pending.token == contract.token:
                self._pending.pop(user_id, None)

    def drop_token(self, token: str | None) -> None:
        contract = self.get(token)
        if contract:
            self.drop(contract)

    # -- pending input ---------------------------------------------------

    def set_pending(
        self, user_id: int, contract: Contract, field_key: str
    ) -> PendingSignature:
        pending = PendingSignature(
            token=contract.token,
            field_key=field_key,
            user_id=user_id,
            deadline=time.time() + self.pending_seconds,
        )
        self._pending[user_id] = pending
        contract.touch()
        return pending

    def clear_pending(self, user_id: int | None = None) -> None:
        if user_id is None:
            self._pending.clear()
        else:
            self._pending.pop(user_id, None)

    def pending_for_contract(self, token: str) -> list[PendingSignature]:
        return [pending for pending in self._pending.values() if pending.token == token]

    def expire_pending(self, now: float | None = None) -> list[PendingSignature]:
        now = now or time.time()
        expired: list[PendingSignature] = []
        for user_id, pending in list(self._pending.items()):
            if pending.expired(now):
                expired.append(pending)
                self._pending.pop(user_id, None)
        return expired

    # -- signatures ------------------------------------------------------

    def add_signature(
        self,
        contract: Contract,
        *,
        field_key: str,
        text: str,
        user_id: int,
        user_name: str = "",
        user_username: str = "",
    ) -> Signature:
        signature = Signature(
            field_key=field_key,
            text=text,
            user_id=user_id,
            user_name=user_name,
            user_username=user_username,
            at=time.time(),
        )
        contract.signatures[field_key] = signature
        self.clear_pending(user_id)
        contract.touch()
        return signature

    # -- housekeeping ----------------------------------------------------

    def _enforce_cap(self) -> None:
        if len(self._by_token) <= self.max_active:
            return
        ordered = sorted(self._by_token.values(), key=lambda item: item.updated_at)
        for contract in ordered[: len(self._by_token) - self.max_active]:
            self.drop(contract)
        now = time.time()
        for user_id, pending in list(self._pending.items()):
            if pending.expired(now) or self.get(pending.token) is None:
                self._pending.pop(user_id, None)

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
        self._chat_tokens.clear()
        self._by_inline.clear()
        self._pending.clear()


def chat_key(chat_id: int, thread_id: int | None = None, *, guest: bool = True) -> str:
    prefix = "g" if guest else "c"
    if thread_id:
        return f"{prefix}{chat_id}:{thread_id}"
    return f"{prefix}{chat_id}"
