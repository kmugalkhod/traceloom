"""Full-mode wiring: S3 blobs, Redis queue/limiter/cache and the ClickHouse TraceStore.

`build_container` imports this module only when TRACELOOM_MODE=full, so lite mode never loads
boto3, redis, dramatiq or clickhouse-connect. The web process only enqueues; the worker process
(`traceloom worker`) calls `container.start_consumer()` to begin consuming.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceloom.container import Container

log = logging.getLogger("traceloom")


def configure(container: Container) -> None:
    import clickhouse_connect
    import redis

    from traceloom.adapters.full.clickhouse_trace_store import (
        ClickHouseTraceStore,
        apply_migrations,
        apply_schema,
    )
    from traceloom.adapters.full.redis_limiter import RedisCache, RedisRateLimiter
    from traceloom.adapters.full.redis_queue import RedisQueue
    from traceloom.adapters.full.s3_blob import S3BlobStore
    from traceloom.ingest.sweeper import Sweeper

    s = container.settings
    redis_client = redis.Redis.from_url(s.redis_url)
    ch_client = clickhouse_connect.get_client(
        host=s.clickhouse_host,
        port=s.clickhouse_port,
        username=s.clickhouse_user,
        password=s.clickhouse_password,
    )
    blobs = S3BlobStore(
        s.s3_bucket,
        endpoint_url=s.s3_endpoint_url,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        region=s.s3_region,
    )
    container.blob_store = blobs
    container.queue = RedisQueue(s.redis_url)
    container.limiter = RedisRateLimiter(redis_client)
    container.cache = RedisCache(redis_client)
    container.trace_store = ClickHouseTraceStore(ch_client, s.clickhouse_database)
    container.sweeper = Sweeper(
        blobs,
        container.queue,
        min_age_seconds=s.ingest_sweep_seconds,
        interval_seconds=s.ingest_sweep_seconds,
    )

    def prepare(c: Container) -> None:
        blobs.ensure_bucket()
        apply_schema(ch_client, s.clickhouse_database)
        apply_migrations(ch_client, s.clickhouse_database)

    def start_consumer() -> None:
        from traceloom.container import register_webhook_handler
        from traceloom.ingest.process import process_message

        register_webhook_handler(container)
        container.queue.start(lambda message: process_message(container, message))
        container.sweeper.run_once(min_age=0)
        container.sweeper.start()

    container.start_consumer = start_consumer  # type: ignore[attr-defined]
    container.startup_hooks.append(prepare)
    container.shutdown_hooks.append(lambda c: (c.sweeper.stop(), c.queue.stop()))
    container.shutdown_hooks.append(lambda c: (ch_client.close(), redis_client.close()))
    container.health_checks["redis"] = lambda: bool(redis_client.ping())
    container.health_checks["blob"] = blobs.ping
    container.health_checks["queue"] = container.queue.ping
    container.health_checks["clickhouse"] = lambda: bool(ch_client.command("SELECT 1"))
