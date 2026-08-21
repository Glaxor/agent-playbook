Implement the following feature batch in the `stress` package. Work through the
list in order; commit after each feature with a conventional-commit message.
Be idempotent: if a feature already exists and its tests pass, skip it.

1. **`stress.core.retry`** — a decorator `retry(attempts: int, backoff: float)`
   that retries a callable on any exception with exponential backoff, fully
   typed (no `Any` in the public signature), with tests covering success on
   the Nth attempt and exhaustion.

2. **`stress.core.ratelimit`** — a `RateLimiter` class enforcing N calls per
   sliding window, thread-safe, typed, with tests that do not sleep for more
   than 0.5s total (inject a fake clock).

3. **`stress.cli`** — a `python -m stress` entry point exposing the two
   utilities behind `retry` and `limit` subcommands, with `--help` text,
   plus tests driving it via `subprocess`.

Every commit must keep `python -m pytest -q` green and
`python -m mypy src/stress --strict` clean. Do not weaken or delete any
existing test, and do not modify the verification commands themselves.
