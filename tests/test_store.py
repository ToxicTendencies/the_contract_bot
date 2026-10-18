from __future__ import annotations

import time

from app.store import ContractStore, STATE_PREVIEW, STATE_TERMS


def make_store(**overrides) -> ContractStore:
    params = {"ttl_seconds": 3600, "pending_seconds": 600, "max_active": 10}
    params.update(overrides)
    return ContractStore(**params)


def test_create_and_lookup():
    store = make_store()
    contract = store.create(
        chat_key="g1", template_key="classic", initiator_id=10, initiator_name="A"
    )
    assert store.get(contract.token) is contract
    assert store.for_chat("g1") is contract
    assert contract.state == STATE_PREVIEW


def test_contracts_in_chat_coexist():
    """Новое сообщение в чате не убивает кнопки предыдущего."""
    store = make_store()
    first = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    second = store.create(chat_key="g1", template_key="minimal", initiator_id=10)
    assert store.get(first.token) is first
    assert store.get(second.token) is second
    assert store.for_chat("g1") is second


def test_chat_keeps_limited_history():
    store = make_store()
    contracts = [
        store.create(chat_key="g1", template_key="classic", initiator_id=10)
        for _ in range(store.max_per_chat + 2)
    ]
    assert store.get(contracts[0].token) is None
    assert store.get(contracts[1].token) is None
    assert store.get(contracts[-1].token) is contracts[-1]
    assert store.for_chat("g1") is contracts[-1]


def test_dropped_contract_leaves_chat():
    store = make_store()
    first = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    store.drop(first)
    assert store.for_chat("g1") is None
    second = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    assert store.for_chat("g1") is second


def test_different_chats_coexist():
    store = make_store()
    a = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    b = store.create(chat_key="g2", template_key="classic", initiator_id=20)
    assert store.get(a.token) is a
    assert store.get(b.token) is b


def test_pending_expires():
    store = make_store()
    contract = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    pending = store.set_pending(10, contract, "party1")
    assert store.pending_for(10) is pending
    expired = store.expire_pending(now=time.time() + 601)
    assert pending in expired
    assert store.pending_for(10) is None


def test_pending_is_per_contract_and_chat():
    """Один человек может подписывать контракты в разных чатах одновременно."""
    store = make_store()
    a = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    b = store.create(chat_key="g2", template_key="classic", initiator_id=10)
    pending_a = store.set_pending(10, a, "party1")
    pending_b = store.set_pending(10, b, "party1")

    assert store.pending_for(10, "g1") is pending_a
    assert store.pending_for(10, "g2") is pending_b
    assert store.pending_for(10, token=a.token) is pending_a
    assert store.pending_for(10, "g3") is None
    assert store.pending_for(10) in (pending_a, pending_b)

    store.clear_pending(10, a.token)
    assert store.pending_for(10, "g1") is None
    assert store.pending_for(10, "g2") is pending_b


def test_drop_clears_only_its_own_pending():
    store = make_store()
    a = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    b = store.create(chat_key="g2", template_key="classic", initiator_id=10)
    store.set_pending(10, a, "party1")
    pending_b = store.set_pending(10, b, "party1")

    store.drop(a)
    assert store.pending_for(10) is pending_b


def test_trim_keeps_contracts_with_signatures():
    """Лимит чата не должен стирать уже подписанный бланк."""
    store = make_store()
    signed = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    store.add_signature(signed, field_key="party1", text="A", user_id=10)
    empty = [
        store.create(chat_key="g1", template_key="classic", initiator_id=10)
        for _ in range(store.max_per_chat)
    ]

    assert store.get(signed.token) is signed
    assert store.get(empty[0].token) is None
    assert len(store._chat_tokens["g1"]) == store.max_per_chat


def test_pending_not_expired_yet():
    store = make_store()
    contract = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    pending = store.set_pending(10, contract, "party1")
    assert store.expire_pending(now=pending.deadline - 1) == []
    assert store.pending_for(10) is not None


def test_ttl_removes_contract():
    store = make_store(ttl_seconds=60)
    contract = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    stale = store.pop_stale(now=contract.updated_at + 61)
    assert contract in stale
    assert store.get(contract.token) is None


def test_cap_drops_oldest():
    store = make_store(max_active=2)
    first = store.create(chat_key="g1", template_key="classic", initiator_id=1)
    second = store.create(chat_key="g2", template_key="classic", initiator_id=2)
    second.updated_at = first.updated_at + 5
    third = store.create(chat_key="g3", template_key="classic", initiator_id=3)
    assert len(store._by_token) == 2
    assert store.get(first.token) is None
    assert store.get(second.token) is second
    assert store.get(third.token) is third


def test_signatures_and_same_signers():
    store = make_store()
    contract = store.create(chat_key="g1", template_key="classic", initiator_id=10)
    store.add_signature(contract, field_key="party1", text="A", user_id=10, user_name="A")
    store.add_signature(contract, field_key="party2", text="B", user_id=10, user_name="A")
    assert contract.same_signers()

    contract2 = store.create(chat_key="g2", template_key="classic", initiator_id=10)
    store.add_signature(contract2, field_key="party1", text="A", user_id=10, user_name="A")
    store.add_signature(contract2, field_key="party2", text="B", user_id=20, user_name="B")
    assert not contract2.same_signers()


def test_terms_pending_is_per_chat_and_user():
    store = make_store()
    contract = store.create(chat_key="g1", template_key="free", initiator_id=10)
    contract.state = STATE_TERMS
    contract.terms_user_id = 10

    assert store.terms_for_user(10, "g1") is contract
    assert store.terms_for_user(11, "g1") is None
    assert store.terms_for_user(10, "g2") is None

    store.set_terms(contract, "Оплата 1000 ₽")
    assert contract.terms == "Оплата 1000 ₽"
    assert contract.state == "open"
    assert contract.terms_user_id == 0
    assert store.terms_for_user(10, "g1") is None
