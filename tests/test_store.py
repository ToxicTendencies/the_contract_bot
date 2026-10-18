from __future__ import annotations

import time

from app.store import ContractStore, STATE_PREVIEW


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
