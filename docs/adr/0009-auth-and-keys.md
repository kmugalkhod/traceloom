# ADR-0009: Sessions, password hashing and API-key hashing

Status: accepted · Date: 2026-11-06

## Decision
- Passwords use argon2id (`argon2-cffi`).
- Browser sessions use a signed cookie (itsdangerous) holding `user_id` and `session_version`. Bumping the version on password change or reset logs out every session.
- Project secret keys have 32 random bytes of entropy, so they are stored as `HMAC-SHA256(key, TRACELOOM_SECRET_KEY)`. A slow hash is not needed for high-entropy secrets, and HMAC allows an O(1) lookup on every ingestion call. A Redis/in-memory cache maps `hash → project_id` for `TRACELOOM_KEY_CACHE_SECONDS`. Revoking a key evicts it from the cache.
- Rate limits are fixed-window: auth endpoints per IP+email, ingestion per key. All limits come from env.
