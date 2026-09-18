"""
Retry utilities for handling transient errors.

Used for:
- SQLite 'database is locked' errors
- Telegram API temporary errors
- Crypto Pay API temporary errors
"""

from __future__ import annotations

import asyncio
import logging
import functools
from typing import TypeVar, Callable, Any

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryError(Exception):
    """All retry attempts exhausted."""
    pass


async def retry_async(
    func: Callable[..., Any],
    *args: Any,
    max_attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    exceptions: tuple = (Exception,),
    **kwargs: Any,
) -> Any:
    """
    Retry an async function with exponential backoff.

    Args:
        func: Async callable to retry
        max_attempts: Maximum number of attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        exceptions: Tuple of exception types to catch
    """
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except exceptions as e:
            last_error = e
            if attempt == max_attempts:
                logger.error(
                    f"All {max_attempts} retry attempts exhausted for {func.__name__}: {e}"
                )
                raise RetryError(
                    f"Failed after {max_attempts} attempts: {e}"
                ) from e

            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning(
                f"Attempt {attempt}/{max_attempts} for {func.__name__} "
                f"failed: {e}. Retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)

    raise RetryError(f"Unexpected: {last_error}")


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    exceptions: tuple = (Exception,),
):
    """Decorator for async functions with retry logic."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_async(
                func, *args,
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                exceptions=exceptions,
                **kwargs,
            )
        return wrapper
    return decorator


async def retry_on_locked(func: Callable, *args, **kwargs) -> Any:
    """
    Retry specifically for SQLite 'database is locked' errors.
    5 attempts, 10s busy_timeout already set in PRAGMA.
    """
    import sqlite3
    return await retry_async(
        func, *args,
        max_attempts=5,
        base_delay=0.2,
        max_delay=5.0,
        exceptions=(sqlite3.OperationalError,),
        **kwargs,
    )
