import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_foundry.catalog import Catalog
from agent_foundry.worker import Worker


async def test_evaluation_runs_while_build_is_blocked(settings):
    started, evaluated, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    claimed = set()

    async def claim(kinds=None, *, exclude=False):
        kind = "BUILD" if exclude else "EVALUATE"
        if kind in claimed:
            return None
        claimed.add(kind)
        return {"kind": kind}

    async def process(job):
        if job["kind"] == "BUILD":
            started.set()
            await release.wait()
        else:
            await started.wait()
            evaluated.set()

    queue = SimpleNamespace(claim=claim, db=SimpleNamespace(event=AsyncMock()))
    deployment = SimpleNamespace(databases=SimpleNamespace(reap_tests=AsyncMock()))
    worker = Worker(queue, None, None, deployment, settings)
    worker.process = process
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(evaluated.wait(), 2)
        assert not release.is_set()  # Evaluation completed before the blocked build.
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_overlapping_catalog_syncs_share_success_only(settings):
    catalog = Catalog(None, None, None, None, settings)
    entered, release = asyncio.Event(), asyncio.Event()

    async def sync():
        entered.set()
        await release.wait()
        return {"git_commit": "a" * 40}

    catalog._sync = AsyncMock(side_effect=sync)
    first = asyncio.create_task(catalog.sync())
    await entered.wait()
    second = asyncio.create_task(catalog.sync())
    await asyncio.sleep(0)
    release.set()
    assert (await first) == (await second)
    assert catalog._sync.await_count == 1
    await catalog.sync()  # A later request must check the remote again.
    assert catalog._sync.await_count == 2
    catalog._sync = AsyncMock(side_effect=[ValueError("fetch failed"), {"changed": False}])
    with pytest.raises(ValueError):
        await catalog.sync()
    assert await catalog.sync() == {"changed": False}


@pytest.mark.integration
async def test_queue_lanes_do_not_claim_each_others_jobs(container):
    build = await container.queue.enqueue("BUILD", "lane-build", {})
    evaluate = await container.queue.enqueue("EVALUATE", "lane-evaluate", {})
    ev = await container.queue.claim(["EVALUATE"])
    assert ev["id"] == evaluate
    assert await container.queue.claim(["EVALUATE"]) is None
    work = await container.queue.claim(["EVALUATE"], exclude=True)
    assert work["id"] == build
