CREATE SCHEMA IF NOT EXISTS agent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS agent.programs (
    id uuid NOT NULL, name text NOT NULL, description text NOT NULL, version text NOT NULL,
    program_type text NOT NULL, runtime text NOT NULL, execution_type text NOT NULL,
    endpoint text, method text, entrypoint text, repository text, repository_path text,
    git_commit text, input_schema jsonb NOT NULL, output_schema jsonb NOT NULL,
    selection_rules jsonb NOT NULL, tags text[] NOT NULL, examples jsonb NOT NULL,
    status text NOT NULL, priority integer NOT NULL DEFAULT 0, manifest jsonb NOT NULL,
    search_text text NOT NULL, search_vector tsvector,
    usage_count bigint NOT NULL DEFAULT 0, success_count bigint NOT NULL DEFAULT 0,
    failure_count bigint NOT NULL DEFAULT 0, avg_latency_ms double precision NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS programs_id ON agent.programs (id);
CREATE INDEX IF NOT EXISTS programs_name ON agent.programs (name);
CREATE INDEX IF NOT EXISTS programs_search ON agent.programs USING gin(search_vector);
CREATE INDEX IF NOT EXISTS programs_trigram ON agent.programs USING gin(search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS programs_status ON agent.programs (status, priority DESC);
CREATE TABLE IF NOT EXISTS agent.releases (
    program_id uuid NOT NULL, version text NOT NULL, git_commit text NOT NULL,
    manifest jsonb NOT NULL, repository text NOT NULL, repository_path text NOT NULL,
    evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS releases_lookup ON agent.releases (program_id, git_commit);
CREATE TABLE IF NOT EXISTS agent.jobs (
    id uuid NOT NULL, kind text NOT NULL, fingerprint text NOT NULL, payload text,
    status text NOT NULL DEFAULT 'PENDING', attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz,
    owner uuid, result jsonb, error text,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_id ON agent.jobs (id);
CREATE INDEX IF NOT EXISTS jobs_claim ON agent.jobs (status, available_at, created_at);
CREATE INDEX IF NOT EXISTS jobs_dedup ON agent.jobs (kind, fingerprint);
CREATE TABLE IF NOT EXISTS agent.events (
    id uuid NOT NULL, request_id uuid, event_type text NOT NULL, data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_request ON agent.events (request_id, created_at);
CREATE INDEX IF NOT EXISTS events_type ON agent.events (event_type, created_at);
CREATE TABLE IF NOT EXISTS agent.tool_migrations (
    program_id uuid NOT NULL, checksum text NOT NULL, git_commit text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_migrations_lookup ON agent.tool_migrations(program_id, checksum);
CREATE TABLE IF NOT EXISTS agent.console_settings (
    name text NOT NULL, encrypted_payload text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS console_settings_name ON agent.console_settings(name);
CREATE TABLE IF NOT EXISTS agent.web_sessions (
    token_hash text NOT NULL, role text NOT NULL, csrf_token text NOT NULL,
    expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS web_sessions_token ON agent.web_sessions(token_hash);
CREATE TABLE IF NOT EXISTS agent.web_launches (
    token_hash text NOT NULL, expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS web_launches_token ON agent.web_launches(token_hash);
ALTER TABLE agent.programs ADD COLUMN IF NOT EXISTS installed_at timestamptz;
ALTER TABLE agent.programs ADD COLUMN IF NOT EXISTS last_deployed_at timestamptz;
ALTER TABLE agent.programs ADD COLUMN IF NOT EXISTS estimated_tokens_saved bigint NOT NULL DEFAULT 0;
ALTER TABLE agent.programs ADD COLUMN IF NOT EXISTS attributed_llm_tokens bigint NOT NULL DEFAULT 0;
ALTER TABLE agent.programs ADD COLUMN IF NOT EXISTS savings_sample_count bigint NOT NULL DEFAULT 0;
ALTER TABLE agent.programs ADD COLUMN IF NOT EXISTS creation_tokens bigint;
UPDATE agent.programs SET installed_at=created_at,last_deployed_at=updated_at
    WHERE status='ACTIVE' AND installed_at IS NULL;
CREATE TABLE IF NOT EXISTS agent.catalog (
    id uuid NOT NULL, name text NOT NULL, description text NOT NULL, version text NOT NULL,
    runtime text NOT NULL, execution_type text NOT NULL, manifest jsonb NOT NULL,
    input_schema jsonb NOT NULL, output_schema jsonb NOT NULL, tags text[] NOT NULL, examples jsonb NOT NULL,
    search_text text NOT NULL, search_vector tsvector, priority integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'PUBLISHED', repository text NOT NULL, repository_path text NOT NULL,
    git_commit text NOT NULL, source_tree text NOT NULL,
    published_at timestamptz NOT NULL, discovered_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS catalog_id ON agent.catalog(id);
CREATE INDEX IF NOT EXISTS catalog_search ON agent.catalog USING gin(search_vector);
CREATE INDEX IF NOT EXISTS catalog_trigram ON agent.catalog USING gin(search_text gin_trgm_ops);
ALTER TABLE agent.catalog ADD COLUMN IF NOT EXISTS creation_tokens bigint;
CREATE TABLE IF NOT EXISTS agent.catalog_sync (
    repository text NOT NULL, git_commit text NOT NULL, synced_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS catalog_sync_repository ON agent.catalog_sync(repository);
CREATE TABLE IF NOT EXISTS agent.request_usage (
    request_id uuid NOT NULL, prompt_hash text NOT NULL, route text NOT NULL,
    actual_llm_tokens bigint, baseline_tokens bigint NOT NULL, estimated_tokens_saved bigint,
    baseline_method text NOT NULL, program_ids jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS request_usage_id ON agent.request_usage(request_id);
CREATE TABLE IF NOT EXISTS agent.prompt_baselines (
    prompt_hash text NOT NULL, model text NOT NULL, total_tokens bigint NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prompt_baselines_lookup ON agent.prompt_baselines(prompt_hash,observed_at DESC);
CREATE TABLE IF NOT EXISTS agent.program_databases (
    program_id uuid NOT NULL, connection_name text NOT NULL DEFAULT 'central',
    schema_name text NOT NULL, role_name text NOT NULL, secret_name text NOT NULL,
    encrypted_credentials text NOT NULL, status text NOT NULL DEFAULT 'READY',
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS program_databases_program ON agent.program_databases(program_id);
CREATE TABLE IF NOT EXISTS agent.database_test_scopes (
    id uuid NOT NULL, schema_name text NOT NULL, role_name text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS database_test_scopes_expiry ON agent.database_test_scopes(expires_at);
