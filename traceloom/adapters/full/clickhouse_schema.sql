-- Traceloom trace data in ClickHouse: the same columns as the lite SQL tables.
-- ReplacingMergeTree(event_ts) keeps the row with the highest event_ts per sort key; reads use FINAL.
-- JSON values (input, output, metadata, model_parameters, usage_details, cost_details) are stored as
-- JSON text. Search uses ngram bloom-filter skip indexes over lower(text) so LIKE '%word%' can skip granules.
-- Statements are separated by a semicolon at the end of a line.

CREATE TABLE IF NOT EXISTS traces
(
    project_id String,
    id String,
    name Nullable(String),
    timestamp DateTime64(3, 'UTC'),
    input Nullable(String),
    output Nullable(String),
    user_id Nullable(String),
    session_id Nullable(String),
    tags Array(String),
    metadata String DEFAULT '{}',
    release Nullable(String),
    version Nullable(String),
    environment LowCardinality(String) DEFAULT 'default',
    public Bool DEFAULT false,
    bookmarked Bool DEFAULT false,
    event_ts DateTime64(3, 'UTC'),
    latency_ms Nullable(Float64),
    total_tokens Int64 DEFAULT 0,
    total_cost Float64 DEFAULT 0,
    max_level LowCardinality(String) DEFAULT 'DEFAULT',
    observation_count Int32 DEFAULT 0,
    search_text String DEFAULT '',
    INDEX idx_search lower(search_text) TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 4,
    INDEX idx_name lower(coalesce(name, '')) TYPE ngrambf_v1(3, 16384, 3, 0) GRANULARITY 4,
    INDEX idx_id lower(id) TYPE ngrambf_v1(3, 16384, 3, 0) GRANULARITY 4,
    INDEX idx_timestamp timestamp TYPE minmax GRANULARITY 1,
    INDEX idx_session coalesce(session_id, '') TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_user coalesce(user_id, '') TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(event_ts)
ORDER BY (project_id, id);

CREATE TABLE IF NOT EXISTS observations
(
    project_id String,
    id String,
    trace_id String,
    parent_observation_id Nullable(String),
    type LowCardinality(String) DEFAULT 'span',
    name Nullable(String),
    start_time DateTime64(3, 'UTC'),
    end_time Nullable(DateTime64(3, 'UTC')),
    completion_start_time Nullable(DateTime64(3, 'UTC')),
    input Nullable(String),
    output Nullable(String),
    metadata String DEFAULT '{}',
    level LowCardinality(String) DEFAULT 'DEFAULT',
    status_message Nullable(String),
    version Nullable(String),
    environment LowCardinality(String) DEFAULT 'default',
    model Nullable(String),
    model_parameters String DEFAULT '{}',
    usage_details String DEFAULT '{}',
    cost_details String DEFAULT '{}',
    cost_source LowCardinality(String) DEFAULT 'none',
    usage_source LowCardinality(String) DEFAULT 'none',
    prompt_name Nullable(String),
    prompt_version Nullable(Int32),
    event_ts DateTime64(3, 'UTC'),
    INDEX idx_start start_time TYPE minmax GRANULARITY 1,
    INDEX idx_prompt_name coalesce(prompt_name, '') TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(event_ts)
ORDER BY (project_id, trace_id, id);

CREATE TABLE IF NOT EXISTS scores
(
    project_id String,
    id String,
    trace_id Nullable(String),
    observation_id Nullable(String),
    session_id Nullable(String),
    dataset_run_id Nullable(String),
    name String,
    value Nullable(Float64),
    string_value Nullable(String),
    data_type LowCardinality(String) DEFAULT 'NUMERIC',
    comment Nullable(String),
    source LowCardinality(String) DEFAULT 'API',
    config_id Nullable(String),
    author_id Nullable(String),
    timestamp DateTime64(3, 'UTC'),
    environment LowCardinality(String) DEFAULT 'default',
    event_ts DateTime64(3, 'UTC'),
    INDEX idx_trace coalesce(trace_id, '') TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(event_ts)
ORDER BY (project_id, id);
