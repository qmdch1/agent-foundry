import asyncio
import contextlib


class Worker:
    def __init__(self, queue, evaluator, builder, deployment, settings):
        self.queue, self.evaluator, self.builder = queue, evaluator, builder
        self.deployment, self.settings = deployment, settings

    async def process(self, job):
        async def dispatch():
            payload = self.queue.payload(job)
            if job["kind"] == "EVALUATE":
                return await self.evaluator.evaluate(payload)
            if job["kind"] == "BUILD":
                return await self.builder.build(payload)
            if job["kind"] == "RECONCILE":
                results = await self.deployment.reconcile()
                return ("SUCCEEDED" if all(r["success"] for r in results) else "FAILED"), {
                    "programs": results
                }
            if job["kind"] == "ROLLBACK":
                await self.deployment.rollback(payload["program_id"], payload["commit"])
                return "SUCCEEDED", {"program_id": payload["program_id"], "git_commit": payload["commit"]}
            raise ValueError("Unknown job kind")

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
        while True:
            job = await self.queue.claim()
            if job:
                await self.process(job)
            if once:
                return bool(job)
            if not job:
                await asyncio.sleep(self.settings.queue_poll_seconds)
