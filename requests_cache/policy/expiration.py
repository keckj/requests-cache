"""Utility functions for parsing and converting expiration values"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from fnmatch import fnmatch
from logging import getLogger
from math import ceil
from typing import Optional

from .._utils import try_int
from . import ExpirationPatterns, ExpirationTime

# Special expiration values that may be set by either headers or keyword args
DO_NOT_CACHE = 0x0D0E0200020704  # Per RFC 4824
EXPIRE_IMMEDIATELY = 0
NEVER_EXPIRE = -1

logger = getLogger(__name__)


def get_expiration_datetime(expire_after: ExpirationTime) -> Optional[datetime]:
    """Convert an expiration value in any supported format to an absolute datetime"""
    # Never expire (or do not cache, in which case expiration won't be used)
    if expire_after is None or expire_after in [NEVER_EXPIRE, DO_NOT_CACHE]:
        return None
    # Expire immediately
    elif try_int(expire_after) == EXPIRE_IMMEDIATELY:
        return datetime.utcnow()
    # Already a datetime or datetime str
    if isinstance(expire_after, str):
        return _parse_http_date(expire_after)
    elif isinstance(expire_after, datetime):
        return _to_utc(expire_after)

    # Otherwise, it must be a timedelta or time in seconds
    if not isinstance(expire_after, timedelta):
        expire_after = timedelta(seconds=expire_after)
    return datetime.utcnow() + expire_after


def get_expiration_seconds(expire_after: ExpirationTime) -> int:
    """Convert an expiration value in any supported format to an expiration time in seconds"""
    if expire_after == DO_NOT_CACHE:
        return DO_NOT_CACHE
    expires = get_expiration_datetime(expire_after)
    return ceil((expires - datetime.utcnow()).total_seconds()) if expires else NEVER_EXPIRE


def get_url_expiration(
    url: Optional[str], urls_expire_after: ExpirationPatterns = None
) -> ExpirationTime:
    """Check for a matching per-URL expiration, if any"""
    if not url:
        return None

    for pattern, expire_after in (urls_expire_after or {}).items():
        if _url_match(url, pattern):
            logger.debug(f'URL {url} matched pattern "{pattern}": {expire_after}')
            return expire_after
    return None


def _parse_http_date(value: str) -> Optional[datetime]:
    """Attempt to parse an HTTP (RFC 5322-compatible) timestamp"""
    try:
        expire_after = parsedate_to_datetime(value)
        return _to_utc(expire_after)
    except (TypeError, ValueError):
        logger.debug(f'Failed to parse timestamp: {value}')
        return None


def _to_utc(dt: datetime):
    """All internal datetimes are UTC and timezone-naive. Convert any user/header-provided
    datetimes to the same format.
    """
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc)
        dt = dt.replace(tzinfo=None)
    return dt


def _url_match(url: str, pattern: str) -> bool:
    """Determine if a URL matches a pattern

    Args:
        url: URL to test. Its base URL (without protocol) will be used.
        pattern: Glob pattern to match against. A recursive wildcard will be added if not present

    Example:
        >>> url_match('https://httpbin.org/delay/1', 'httpbin.org/delay')
        True
        >>> url_match('https://httpbin.org/stream/1', 'httpbin.org/*/1')
        True
        >>> url_match('https://httpbin.org/stream/2', 'httpbin.org/*/1')
        False
    """
    url = url.split('://')[-1]
    pattern = pattern.split('://')[-1].rstrip('*') + '**'
    return fnmatch(url, pattern)
