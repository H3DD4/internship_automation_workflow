"""
Tiny generic retry helper with exponential backoff + jitter.

Used to make the scraping request and the two Claude API calls resilient to
one-off transient blips (a timeout, a connection reset, a 429/5xx) instead
of immediately giving up and permanently falling back to a degraded
"unknown" result for a company that would otherwise have researched fine.

Deliberately dependency-free (stdlib only) to keep the project lightweight.
"""

import time
import random


def with_retry(fn, *, attempts: int = 3, base_delay: float = 1.5,
                retry_on=(Exception,), what: str = "operation", quiet: bool = False):
    """
    Calls fn() and returns its result. On an exception matching `retry_on`,
    retries up to `attempts` times with exponential backoff + jitter.
    Re-raises the last exception if every attempt fails.
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as e:
            last_err = e
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            if not quiet:
                print(f"    [retry] {what} failed (attempt {attempt}/{attempts}): {e} "
                      f"— retrying in {delay:.1f}s")
            time.sleep(delay)
    raise last_err
