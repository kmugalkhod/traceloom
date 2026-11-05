# ADR-0005: Code-evaluator sandbox (decided now, built in Phase 6)

Status: accepted · Date: 2026-11-06

## Decision
Each evaluation runs in a fresh `python -I` subprocess. Input arrives on stdin as JSON and the result is read from stdout. The process has `RLIMIT_AS` (memory), `RLIMIT_CPU` and a wall-clock timeout (the parent kills its process group), a stripped environment and an empty temp working directory. Network isolation comes from deployment: in full mode the evaluator runs in a worker container with `network_mode: none`. In lite mode on Linux it uses `unshare -n` where available. Otherwise it is documented as best effort.

## Consequences
Errors and timeouts become recorded evaluator failures and never crash the worker.
