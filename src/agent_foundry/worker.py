import asyncio
import contextlib
import time

from .timing import stage


class Worker:
    def __init__(self, queue, evaluator, builder, deployment, settings, catalog=None):
        self.queue, self.evaluator, self.builder = queue, evaluator, builder
        self.deployment, self.settings = deployment, settings
        self.catalog = catalog

    async def process(self, job):
        async def dispatch_inner():
            payload = self.queue.payload(job)
            if job["kind"] == "EVALUATE":
                return await self.evaluator.evaluate(payload)
            if job["kind"] == "BUILD":
                return await self.builder.build(payload)
            if job["kind"] == "INSTALL":
                return await self.catalog.install(payload["references"])
            if job["kind"] == "CATALOG_SYNC":
                return "SUCCEEDED", await self.catalog.sync()
            if job["kind"] == "DISCOVER":
                reused = await self.catalog.reuse(payload["prompt"], payload.get("request_id"))
                return reused or ("SKIPPED", {"reason": "no_published_match_build_disabled"})
            if job["kind"] == "RECONCILE":
                results = await self.deployment.reconcile()
                return ("SUCCEEDED" if all(r["success"] for r in results) else "FAILED"), {
                    "programs": results
                }
            if job["kind"] == "ROLLBACK":
                await self.deployment.rollback(payload["program_id"], payload["commit"])
                return "SUCCEEDED", {"program_id": payload["program_id"], "git_commit": payload["commit"]}
            raise ValueError("Unknown job kind")

        async def dispatch():
            async with stage(self.queue.db, "job", job_id=str(job["id"]), kind=job["kind"]) as details:
                result = await dispatch_inner()
                details["status"] = result[0]
                return result

        async def renew():
            while True:
                await asyncio.sleep(self.settings.job_lease_seconds / 3)
                await self.queue.heartbeat(job)

        work, heartbeat = asyncio.create_task(dispatch()), asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                heartbeat.result()  # Raises and cancels work if ownership is lost.
            status, result = await work
            await self.queue.finish(job, status, result)
        except Exception as exc:
            await self.queue.finish(job, "FAILED", error=str(exc))
        finally:
            for task in (work, heartbeat):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def run(self, once=False):
        if once or not self.settings.worker_evaluation_concurrency:
            return await self._run_lane(once=once)
        async with asyncio.TaskGroup() as group:
            group.create_task(self._run_lane(kinds=["EVALUATE"], exclude=True))
            for _ in range(self.settings.worker_evaluation_concurrency):
                group.create_task(self._run_lane(kinds=["EVALUATE"], maintenance=False))

    async def _run_lane(self, once=False, *, kinds=None, exclude=False, maintenance=True):
        next_sync = 0
        while True:
            if maintenance and time.monotonic() >= next_sync:
                try:
                    await self.deployment.databases.reap_tests()
                    if self.catalog and self.settings.catalog_enabled:
                        await self.catalog.sync()
                except Exception as exc:
                    await self.queue.db.event("worker_maintenance_failed", {"error": type(exc).__name__})
                next_sync = time.monotonic() + self.settings.catalog_sync_seconds
            job = await self.queue.claim(kinds, exclude=exclude)
            if job:
                await self.process(job)
            if once:
                return bool(job)
            if not job:
                await asyncio.sleep(self.settings.queue_poll_seconds)
