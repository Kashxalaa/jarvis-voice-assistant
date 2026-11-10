import asyncio

from brain.approvals import ApprovalRequest, ApprovalResult, PendingApprovals


def test_request_defaults_to_a_single_item_batch():
    req = ApprovalRequest("pc", "run a command", "dir", "")
    assert req.batch_size == 1


def test_result_is_a_frozen_value():
    result = ApprovalResult(True, "allowed", "owner")
    assert (result.allowed, result.outcome, result.approver) == (True, "allowed", "owner")


def test_create_returns_unique_ids_and_pending_futures():
    async def scenario():
        pending = PendingApprovals()
        id1, fut1 = pending.create()
        id2, fut2 = pending.create()
        assert id1 != id2
        assert not fut1.done() and not fut2.done()

    asyncio.run(scenario())


def test_resolve_allow_completes_the_future():
    async def scenario():
        pending = PendingApprovals()
        approval_id, future = pending.create()
        assert pending.resolve(approval_id, True, "Dad") is True
        assert future.result() == (True, "Dad")

    asyncio.run(scenario())


def test_resolve_deny_completes_the_future():
    async def scenario():
        pending = PendingApprovals()
        approval_id, future = pending.create()
        assert pending.resolve(approval_id, False, "Dad") is True
        assert future.result() == (False, "Dad")

    asyncio.run(scenario())


def test_resolve_twice_returns_false_the_second_time():
    async def scenario():
        pending = PendingApprovals()
        approval_id, future = pending.create()
        assert pending.resolve(approval_id, True, "Dad") is True
        assert pending.resolve(approval_id, False, "Dad") is False
        assert future.result() == (True, "Dad")

    asyncio.run(scenario())


def test_resolve_unknown_id_returns_false():
    async def scenario():
        assert PendingApprovals().resolve("nope", True, "Dad") is False

    asyncio.run(scenario())


def test_only_the_creating_registry_resolves():
    async def scenario():
        local, cloud = PendingApprovals(), PendingApprovals()
        approval_id, future = cloud.create()
        assert local.resolve(approval_id, True, "Dad") is False
        assert not local.knows(approval_id)
        assert cloud.knows(approval_id)
        assert not future.done()

    asyncio.run(scenario())


def test_discard_makes_a_late_resolve_a_noop_but_id_stays_known():
    async def scenario():
        pending = PendingApprovals()
        approval_id, future = pending.create()
        pending.discard(approval_id)
        assert pending.resolve(approval_id, True, "Dad") is False
        assert future.cancelled()
        assert pending.knows(approval_id)

    asyncio.run(scenario())


def test_resolve_from_another_thread_reaches_a_waiter_on_a_different_loop():
    import threading

    pending = PendingApprovals()
    holder = {}
    ready = threading.Event()

    def worker():
        async def waiter():
            approval_id, future = pending.create()
            holder["id"] = approval_id
            ready.set()
            return await asyncio.wait_for(future, 5)

        holder["result"] = asyncio.run(waiter())

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(5)
    assert pending.resolve(holder["id"], True, "Dad") is True
    thread.join(10)
    assert holder["result"] == (True, "Dad")


def test_concurrent_resolves_from_threads_have_exactly_one_winner():
    import threading

    pending = PendingApprovals()
    holder = {}
    ready = threading.Event()

    def worker():
        async def waiter():
            approval_id, future = pending.create()
            holder["id"] = approval_id
            ready.set()
            return await asyncio.wait_for(future, 5)

        holder["result"] = asyncio.run(waiter())

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(5)
    wins = []
    callers = [
        threading.Thread(
            target=lambda n=n: wins.append(pending.resolve(holder["id"], n % 2 == 0, str(n)))
        )
        for n in range(8)
    ]
    for c in callers:
        c.start()
    for c in callers:
        c.join()
    thread.join(10)
    assert wins.count(True) == 1
    assert holder["result"][1] in {str(n) for n in range(8)}
