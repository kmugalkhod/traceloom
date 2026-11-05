# ADR-0003: Dramatiq on Redis for the job queue

Status: accepted · Date: 2026-11-06

## Decision
Use **Dramatiq** with the Redis broker in full mode. Workers are threaded, which means no fork per job and suits small, frequent ingest jobs. It acks after success, has retries with backoff and supports actor time limits. In lite mode an in-process asyncio queue implements the same `Queue` port.

## Alternatives
- RQ: forks a process per job by default, which adds too much overhead at 20+ ingest jobs/s.
- Celery: heavier to configure and run than this needs.
- Raw Redis Streams: would mean writing retries and dead-lettering ourselves.

## Consequences
Durability does not rely on the queue. The blob plus the sweeper is the source of truth (ADR-0002), so a lost Redis message only delays processing.
