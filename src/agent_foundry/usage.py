import json
import math

from psycopg.types.json import Jsonb


class UsageAccounting:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    @staticmethod
    def reported_tokens(data):
        usage = data.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return tokens if type(tokens) is int and 0 <= tokens <= 1_000_000_000 else None

    async def record(self, request_id, fingerprint, prompt, route, program_ids, result):
        events = await self.db.fetch(
            """SELECT data FROM agent.events WHERE request_id=%s AND event_type='llm'
            AND data->>'role' IN ('main','router') ORDER BY created_at""",
            (request_id,),
        )
        reported = [self.reported_tokens(r["data"]) for r in events]
        known = all(n is not None for n in reported)
        actual = sum(reported) if known else None
        # A previous measured LLM answer to the same prompt is the preferred comparison, not another paid call.
        baseline = await self.db.fetch(
            "SELECT total_tokens FROM agent.prompt_baselines WHERE prompt_hash=%s ORDER BY observed_at DESC LIMIT 1",
            (fingerprint,),
        )
        if baseline:
            estimate, method = baseline[0]["total_tokens"], "previous_llm_response"
        else:
            size = len(prompt.encode()) + len(json.dumps(result, ensure_ascii=False).encode())
            estimate = (
                math.ceil(size / self.settings.token_estimate_bytes_per_token)
                + self.settings.token_estimate_overhead
            )
            method = "configured_byte_estimate"
        ids = sorted(set(program_ids))
        saved = max(0, estimate - actual) if ids and actual is not None else (None if ids else 0)
        async with self.db.pool.connection() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("usage:" + str(request_id),)
            )
            if await (
                await conn.execute("SELECT 1 FROM agent.request_usage WHERE request_id=%s", (request_id,))
            ).fetchone():
                return
            await conn.execute(
                """INSERT INTO agent.request_usage
                (request_id,prompt_hash,route,actual_llm_tokens,baseline_tokens,estimated_tokens_saved,baseline_method,program_ids)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (request_id, fingerprint, route, actual, estimate, saved, method, Jsonb(ids)),
            )
            for i, program_id in enumerate(ids):
                # A workflow shares one counterfactual baseline. Do not credit the full savings to every step.
                share = saved // len(ids) + int(i < saved % len(ids)) if saved is not None else 0
                used = actual // len(ids) + int(i < actual % len(ids)) if actual is not None else 0
                await conn.execute(
                    """UPDATE agent.programs SET estimated_tokens_saved=estimated_tokens_saved+%s,
                    attributed_llm_tokens=attributed_llm_tokens+%s,savings_sample_count=savings_sample_count+%s WHERE id=%s""",
                    (share, used, int(saved is not None), program_id),
                )
            if not ids and route not in {"installation_pending", "discovery_pending"}:
                for row in events:
                    data = row["data"]
                    tokens = self.reported_tokens(data)
                    if data["role"] == "main" and data.get("success") and type(tokens) is int and tokens >= 0:
                        await conn.execute(
                            "INSERT INTO agent.prompt_baselines(prompt_hash,model,total_tokens) VALUES (%s,%s,%s)",
                            (fingerprint, data["model"], tokens),
                        )
        return {
            "actual_llm_tokens": actual,
            "baseline_tokens": estimate,
            "estimated_tokens_saved": saved,
            "baseline_method": method,
            "is_estimate": True,
        }
