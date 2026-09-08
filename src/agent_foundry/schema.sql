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
