# ADR-0012: Webhook delivery

Status: accepted · Date: 2026-11-06

## Context
Teams want to react when a prompt label moves or a version is created (for example to trigger a deploy or a cache purge). Delivery must not slow down or fail the change that caused it, and receivers need to trust and de-duplicate what they get.

## Decision
- **Events are rows first.** When a label moves or a version is created, the service writes one `webhook_deliveries` row (status `pending`) for each enabled webhook subscribed to that event, in the same transaction as the change. This happens inside a savepoint and never raises, so a webhook problem cannot fail a label move. Events are `prompt.label_moved` and `prompt.version_created`.
- **Queued, at-least-once delivery.** A worker handler picks pending rows from the queue and sends them. Queue messages carry a `type` discriminator (`ingest` by default, `webhook_delivery`) and both the in-process and the Redis queue dispatch on it. In lite mode the sweeper replays due deliveries from the database after a restart, because retries are driven by each row's own `next_attempt_at`.
- **Idempotent for receivers.** Each delivery has a stable id sent as `X-Traceloom-Delivery`, and the payload is `{id, event, created_at, project_id, data}`. A receiver can de-duplicate on the id.
- **Signed bodies.** The secret is shown once and stored encrypted (ADR-0011). Each request carries `X-Traceloom-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`. Including the timestamp lets receivers reject replays.
- **Retries.** Network errors, timeouts and 5xx responses are retried, 5 attempts in total with exponential backoff and jitter and a 10 second timeout per attempt. Other 4xx responses fail the delivery without retry. Each attempt updates the attempt count, last status code and last error, which the webhook page shows as a delivery log, next to a "Send test" button.
- **SSRF guard at save and at every delivery.** URLs must be `https://` (`http://` in dev). The host is resolved and refused if it points at a private, loopback, link-local or metadata address, unless `TRACELOOM_WEBHOOK_ALLOW_PRIVATE=true`. The check runs again before each send and connects to the pinned IP, so DNS changes after save cannot redirect it.

## Consequences
- A receiver outage never blocks or rolls back a label move. Deliveries simply stay pending or end up failed in the log.
- At-least-once means receivers may see duplicates and must use the delivery id.
- The delivery log grows with use. Pruning old rows is left for later.

## Alternatives considered
- Sending inline during the label move: rejected, a slow or failing receiver would block or fail the move.
- A separate scheduler for retries: rejected, the row's `next_attempt_at` plus the existing sweeper reuses what lite mode already has.
