import json

import regex

from .models import Candidate, Manifest, validate_json


def map_input(prompt: str, manifest: Manifest):
    normalized = " ".join(prompt.casefold().split())
    for example in manifest.examples:
        if normalized == " ".join(example.prompt.casefold().split()):
            return example.input
    for rule in manifest.selection_rules:
        try:
            match = regex.fullmatch(rule.pattern, prompt.strip(), regex.IGNORECASE, timeout=0.025)
            if not match:
                continue
            values = {}
            for name, kind in rule.fields.items():
                value = match.group(name)
                values[name] = {"integer": int, "number": float, "json": json.loads, "string": str}[kind](
                    value
                )
            validate_json(values, manifest.input_schema)
            return values
        except (ValueError, KeyError, IndexError, TimeoutError):
            continue
        except Exception:
            # Invalid mappings are never allowed onto the zero-LLM path.
            continue
    return None


class ProgramSearch:
    def __init__(self, db, settings, *, catalog=False):
        self.db, self.settings = db, settings
        self.catalog = catalog

    async def search(self, prompt, *, include_disabled=False):
        # Indexed FTS + trigram prefilter; exact examples include multilingual and arithmetic requests.
        table = "agent.catalog" if self.catalog else "agent.programs"
        status = "PUBLISHED" if self.catalog else "ACTIVE"
        rows = await self.db.fetch(
            f"""WITH q AS (SELECT plainto_tsquery('simple',%s) AS tsq)
            SELECT p.*, LEAST(1.0, GREATEST(similarity(search_text,%s),
                ts_rank_cd(search_vector,q.tsq)/(1+ts_rank_cd(search_vector,q.tsq)))) AS rank
            FROM {table} p,q
            WHERE (status='{status}' OR (%s AND status='DISABLED'))
              AND manifest->>'visibility'='public'
              AND (search_vector @@ q.tsq OR search_text %% %s
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(examples) e
                              WHERE lower(e->>'prompt')=lower(%s))
                   OR name='calculator')
            ORDER BY rank DESC,priority DESC,name LIMIT %s""",
            (
                prompt,
                prompt.lower(),
                include_disabled,
                prompt.lower(),
                prompt,
                self.settings.search_top_k * 4,
            ),
        )
        candidates = []
        for row in rows:
            manifest = Manifest.model_validate(row["manifest"])
            mapped = map_input(prompt, manifest)
            score = 1.0 if mapped is not None else float(row["rank"])
            if score < self.settings.search_min_score:
                continue
            candidates.append(
                Candidate(
                    program_id=row["id"],
                    name=manifest.name,
                    description=manifest.description,
                    input_schema=manifest.input_schema,
                    output_schema=manifest.output_schema,
                    tags=manifest.tags,
                    examples=manifest.examples[:2],
                    score=score,
                    mapped_input=mapped,
                )
            )
        return sorted(candidates, key=lambda c: (-c.score, c.name))[: self.settings.search_top_k]
