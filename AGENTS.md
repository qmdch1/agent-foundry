# Agent Foundry

- Main platform repository: `https://github.com/qmdch1/agent-foundry.git`, folder `agent-foundry`.
- Generated programs belong only to the separate sibling repository `https://github.com/qmdch1/agent-tools.git`, under `tools/<name>`. Never vendor its checkout into this repository.
- Keep API/search/router/executor separate from evaluation/build workers. User responses never wait for tool generation.
- Search this platform's installed Registry first, then the indexed GitHub tool catalog. Refresh the approved Git repository in the separate Worker before creating a missing capability. Import a published suitable tool instead of generating a duplicate. Never equate Git lookup failure with an empty repository.
- Store program purpose, first installation date, latest deployment date, calls, actual attributable LLM usage, and estimated token savings in the main DB. Preserve statistics during upgrades/rollback and never label counterfactual savings as measured usage.
- A pushed immutable tool commit must be discoverable by other platforms with independent empty Registry databases. Share source/manifests through Git only; keep credentials, prompts, operational DB data, and platform-specific usage statistics local. Installation/testing/deployment/publication run in the separate Worker, never the request handler.
- Send only bounded Top-K public metadata to the Router. Resolve endpoints, commands, credentials and deployment locations exclusively in trusted code.
- Prefer deterministic input mapping and zero-LLM execution. Configure thresholds, models, limits and cost weights.
- Use an OpenAI-compatible API, as selected by the user. Configure the provider URL and each role's model; never silently choose a paid model or embed API credentials.
- Maintain an integrated Korean web console in the main FastAPI application for prompts, installed programs, provider connection, and role-specific model settings. Keep desktop and mobile navigation usable. Read live Registry data rather than presenting demo state as installed programs.
- Use server-side encrypted API settings shared by API and Worker processes. Never store provider keys in browser storage or return saved keys to the browser. Distinguish API key authentication from ChatGPT account login/subscriptions; use protected administrator sessions for settings changes.
- Generated Python implements `run(dict) -> dict`. Validate schemas, test in non-root resource-limited containers, publish immutable Git commits, then activate. Failed releases must not replace stable versions.
- No host execution of generated Python; no generated Dockerfiles, shell scripts or arbitrary SQL in the trusted control plane. Network and dependencies require explicit administrator configuration.
- Use PostgreSQL durable leased jobs and ordinary indexes. Do not generate key constraints. Serialize logical identity changes with advisory locks.
- Keep secrets and runtime data out of Git. Record source revisions and sanitized processing evidence. Reconcile and rollback from pinned commits.
- Verify changes with `uv run pytest`, `uv run ruff check .`, and Compose configuration validation. Integration tests use an isolated PostgreSQL database, never existing project databases.
