"""In-memory contract state with TTL cleanup.

Nothing here survives a restart on purpose: contracts are ephemeral, and the
only persistent artefact is the archive of finished images in the owner's chat.

Everything happens in the chat: a contract is bound to the chat key and to the
``inline_message_id`` of the bot's guest message, which is what we edit.  A
signature is collected by replying to that message.

Pending input is tracked per user, so two people can be typing their
signatures at the same time.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

STATE_PREVIEW = "preview"
STATE_TERMS = "terms"
STATE_OPEN = "open"
STATE_SIGNED = "signed"
STATE_CLOSED = "closed"

TOKEN_LENGTH = 8
MAX_TERMS_REVISIONS = 10


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
    chat_key: str | None = None
    deadline: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.deadline


@dataclass
class PendingTerms:
    """Человек пишет новую редакцию условий: оспорил или меняет свои."""

    token: str
    user_id: int
    chat_key: str | None = None
    deadline: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.deadline


@dataclass(frozen=True)
class TermsRevision:
    """Прежняя редакция условий: что было и кто её предложил."""

    text: str
    user_id: int = 0
    user_name: str = ""
    at: float = 0.0


@dataclass
class Contract:
    token: str
    template_key: str
    chat_key: str | None = None
    inline_message_id: str | None = None
    chat_id: int | None = None
    message_id: int | None = None
    # Свободные условия и file_id бланка с уже напечатанными условиями.
    terms: str = ""
    # Кто сейчас должен прислать условия (0 — бот условий не ждёт).
    terms_user_id: int = 0
    # Кто предложил действующую редакцию и что было в прежних.
    terms_author_id: int = 0
    terms_author_name: str = ""
    terms_revisions: list[TermsRevision] = field(default_factory=list)
    blank_file_id: str | None = None
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

    @property
    def has_message(self) -> bool:
        """Есть ли за что редактировать: inline-сообщение или сообщение в чате."""
        return bool(self.inline_message_id or (self.chat_id and self.message_id))

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
        # Один человек может подписывать контракты в нескольких чатах сразу,
        # поэтому ожидание ввода привязано к контракту, а не только к юзеру.
        self._pending: dict[tuple[int, str], PendingSignature] = {}
        # Отдельно — ожидание новой редакции условий: подпись и текст
        # условий не должны перепутаться.
        self._terms_pending: dict[tuple[int, str], PendingTerms] = {}

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

    def open_for_chat(self, chat_key: str) -> Contract | None:
        """Незавершённый контракт чата: его продолжают новым сообщением."""
        for token in reversed(self._chat_tokens.get(chat_key, [])):
            contract = self._by_token.get(token)
            if contract is not None and contract.state != STATE_SIGNED:
                return contract
        return None

    def for_message(self, chat_key: str, message_id: int | None) -> Contract | None:
        """Контракт чата, привязанный к конкретному сообщению бота."""
        if not message_id:
            return None
        for token in reversed(self._chat_tokens.get(chat_key, [])):
            contract = self._by_token.get(token)
            if contract is not None and contract.message_id == message_id:
                return contract
        return None

    def pending_for(
        self,
        user_id: int,
        chat_key: str | None = None,
        *,
        token: str | None = None,
    ) -> PendingSignature | None:
        """Ожидание ввода: в этом чате, по токену или самое свежее.

        ``chat_key`` не даёт ответу в одном чате подписать контракт другого.
        Контракты, восстановленные после рестарта, могут не знать свой чат —
        такие ожидания подходят только как запасной вариант.
        """
        if token is not None:
            pending = self._pending.get((user_id, token))
            if pending is not None and pending.expired():
                self._pending.pop((user_id, token), None)
                return None
            return pending

        now = time.time()
        live: list[PendingSignature] = []
        for key, pending in list(self._pending.items()):
            if pending.expired(now):
                self._pending.pop(key, None)
                continue
            if pending.user_id == user_id:
                live.append(pending)
        if not live:
            return None
        if chat_key is None:
            return max(live, key=lambda item: item.deadline)
        exact = [item for item in live if item.chat_key == chat_key]
        if exact:
            return max(exact, key=lambda item: item.deadline)
        unknown = [item for item in live if item.chat_key is None]
        return max(unknown, key=lambda item: item.deadline) if unknown else None

    def terms_for_user(self, user_id: int, chat_key: str) -> Contract | None:
        """Контракт в этом чате, который ждёт условия от этого пользователя."""
        for token in reversed(self._chat_tokens.get(chat_key, [])):
            contract = self._by_token.get(token)
            if (
                contract is not None
                and contract.state == STATE_TERMS
                and contract.terms_user_id == user_id
            ):
                return contract
        return None

    def pending_holder(self, token: str, field_key: str) -> int | None:
        """Who is currently typing a signature for this field, if anyone."""
        now = time.time()
        for key, pending in list(self._pending.items()):
            if pending.expired(now):
                self._pending.pop(key, None)
                continue
            if pending.token == token and pending.field_key == field_key:
                return pending.user_id
        return None

    def pending_terms_for(
        self,
        user_id: int,
        chat_key: str | None = None,
        *,
        token: str | None = None,
    ) -> PendingTerms | None:
        """Кто сейчас печатает новую редакцию условий."""
        if token is not None:
            pending = self._terms_pending.get((user_id, token))
            if pending is not None and pending.expired():
                self._terms_pending.pop((user_id, token), None)
                return None
            return pending

        now = time.time()
        live: list[PendingTerms] = []
        for key, pending in list(self._terms_pending.items()):
            if pending.expired(now):
                self._terms_pending.pop(key, None)
                continue
            if pending.user_id == user_id:
                live.append(pending)
        if not live:
            return None
        if chat_key is None:
            return max(live, key=lambda item: item.deadline)
        exact = [item for item in live if item.chat_key == chat_key]
        if exact:
            return max(exact, key=lambda item: item.deadline)
        unknown = [item for item in live if item.chat_key is None]
        return max(unknown, key=lambda item: item.deadline) if unknown else None

    def unique_pending_terms(self, user_id: int) -> PendingTerms | None:
        """Единственное ожидание условий у человека (для несовпавших чатов)."""
        now = time.time()
        live: list[PendingTerms] = []
        for key, pending in list(self._terms_pending.items()):
            if pending.expired(now):
                self._terms_pending.pop(key, None)
                continue
            if pending.user_id == user_id:
                live.append(pending)
        return live[0] if len(live) == 1 else None

    def pending_terms_for_contract(self, token: str) -> list[PendingTerms]:
        return [
            pending
            for pending in self._terms_pending.values()
            if pending.token == token and not pending.expired()
        ]

    def unique_pending(self, user_id: int) -> PendingSignature | None:
        """Единственное ожидание ввода у человека — если их ровно одно.

        Нужно как запасной вариант для ответов, у которых не совпал ключ чата
        (тема форума, guest-контекст): подписывать «наугад» чужой контракт
        нельзя, а при одном ожидании ошибиться негде.
        """
        now = time.time()
        live: list[PendingSignature] = []
        for key, pending in list(self._pending.items()):
            if pending.expired(now):
                self._pending.pop(key, None)
                continue
            if pending.user_id == user_id:
                live.append(pending)
        return live[0] if len(live) == 1 else None

    def contracts(self) -> list[Contract]:
        return list(self._by_token.values())

    @property
    def active_count(self) -> int:
        return len(self._by_token)

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._terms_pending)

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
            live = [token for token in tokens if token in self._by_token]
            if not live:
                break
            # Сначала расстаёмся с пустыми бланками: в подписанных уже есть
            # работа людей, и терять её из-за нового вызова бота нельзя.
            empty = [token for token in live if not self._by_token[token].signatures]
            victim = empty[0] if empty else live[0]
            self.drop(self._by_token[victim])

    def bind_inline(self, contract: Contract, inline_message_id: str) -> None:
        if (
            contract.inline_message_id
            and contract.inline_message_id != inline_message_id
        ):
            self._by_inline.pop(contract.inline_message_id, None)
        contract.inline_message_id = inline_message_id
        self._by_inline[inline_message_id] = contract.token
        contract.touch()

    def unbind_inline(self, inline_message_id: str | None) -> None:
        if inline_message_id:
            self._by_inline.pop(inline_message_id, None)

    def bind_message(
        self,
        contract: Contract,
        *,
        chat_id: int,
        message_id: int,
        chat_key: str | None = None,
    ) -> None:
        """Переключить правки на обычное сообщение бота (бот в чате)."""
        self.unbind_inline(contract.inline_message_id)
        contract.inline_message_id = None
        contract.chat_id = chat_id
        contract.message_id = message_id
        if chat_key:
            contract.chat_key = chat_key
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
        self.clear_pending(token=contract.token)
        self.clear_pending_terms(token=contract.token)

    def drop_chat(self, chat_key: str, keep: str | None = None) -> list[Contract]:
        """Забыть прежние контракты чата: живёт только последний."""
        dropped: list[Contract] = []
        for token in list(self._chat_tokens.get(chat_key, [])):
            if keep is not None and token == keep:
                continue
            contract = self._by_token.get(token)
            if contract is not None:
                dropped.append(contract)
                self.drop(contract)
        return dropped

    def drop_token(self, token: str | None) -> None:
        contract = self.get(token)
        if contract:
            self.drop(contract)

    # -- pending input ---------------------------------------------------

    def set_terms(
        self,
        contract: Contract,
        text: str,
        *,
        author_id: int = 0,
        author_name: str = "",
    ) -> None:
        """Новая редакция условий; прежняя остаётся в истории."""
        if contract.terms and contract.terms != text:
            contract.terms_revisions.append(
                TermsRevision(
                    text=contract.terms,
                    user_id=contract.terms_author_id,
                    user_name=contract.terms_author_name,
                    at=time.time(),
                )
            )
            # Контракт живёт час: больше десятка редакций за это время —
            # уже спам, историю незачем копить бесконечно.
            del contract.terms_revisions[:-MAX_TERMS_REVISIONS]
        contract.terms = text
        contract.terms_author_id = author_id
        contract.terms_author_name = author_name
        contract.state = STATE_OPEN
        contract.terms_user_id = 0
        contract.touch()

    def reset_signatures(self, contract: Contract) -> None:
        """Условия изменились — старые подписи больше ничего не значат."""
        contract.signatures.clear()
        self.clear_pending(token=contract.token)
        self.clear_pending_terms(token=contract.token)
        contract.touch()

    def set_pending_terms(self, user_id: int, contract: Contract) -> PendingTerms:
        # Нажал «оспорить» — прежнее ожидание подписи этого человека
        # по этому же контракту больше не нужно.
        self.clear_pending(user_id, contract.token)
        pending = PendingTerms(
            token=contract.token,
            user_id=user_id,
            chat_key=contract.chat_key,
            deadline=time.time() + self.pending_seconds,
        )
        self._terms_pending[(user_id, contract.token)] = pending
        contract.touch()
        return pending

    def clear_pending_terms(
        self, user_id: int | None = None, token: str | None = None
    ) -> None:
        if user_id is None and token is None:
            self._terms_pending.clear()
            return
        for key, pending in list(self._terms_pending.items()):
            if user_id is not None and pending.user_id != user_id:
                continue
            if token is not None and pending.token != token:
                continue
            self._terms_pending.pop(key, None)

    def set_pending(
        self, user_id: int, contract: Contract, field_key: str
    ) -> PendingSignature:
        # Нажатие на другую сторону того же контракта заменяет ожидание,
        # но нажатие в другом чате или на другом бланке его не трогает.
        # Новая редакция условий отменяет ожидание текста условий.
        self.clear_pending_terms(user_id, contract.token)
        pending = PendingSignature(
            token=contract.token,
            field_key=field_key,
            user_id=user_id,
            chat_key=contract.chat_key,
            deadline=time.time() + self.pending_seconds,
        )
        self._pending[(user_id, contract.token)] = pending
        contract.touch()
        return pending

    def clear_pending(self, user_id: int | None = None, token: str | None = None) -> None:
        if user_id is None and token is None:
            self._pending.clear()
            return
        for key, pending in list(self._pending.items()):
            if user_id is not None and pending.user_id != user_id:
                continue
            if token is not None and pending.token != token:
                continue
            self._pending.pop(key, None)

    def pending_for_contract(self, token: str) -> list[PendingSignature]:
        return [pending for pending in self._pending.values() if pending.token == token]

    def expire_pending(
        self, now: float | None = None
    ) -> list[PendingSignature | PendingTerms]:
        now = now or time.time()
        expired: list[PendingSignature | PendingTerms] = []
        for key, pending in list(self._pending.items()):
            if pending.expired(now):
                expired.append(pending)
                self._pending.pop(key, None)
        for key, pending in list(self._terms_pending.items()):
            if pending.expired(now):
                expired.append(pending)
                self._terms_pending.pop(key, None)
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
        self.clear_pending(user_id, contract.token)
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
        for key, pending in list(self._pending.items()):
            if pending.expired(now) or self.get(pending.token) is None:
                self._pending.pop(key, None)
        for key, pending in list(self._terms_pending.items()):
            if pending.expired(now) or self.get(pending.token) is None:
                self._terms_pending.pop(key, None)

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
        self._terms_pending.clear()


def chat_key(chat_id: int, thread_id: int | None = None, *, guest: bool = True) -> str:
    prefix = "g" if guest else "c"
    if thread_id:
        return f"{prefix}{chat_id}:{thread_id}"
    return f"{prefix}{chat_id}"
