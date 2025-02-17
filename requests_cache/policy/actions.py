from datetime import datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING, Dict, Optional, Union

from attr import define, field
from requests import PreparedRequest, Response

from .._utils import coalesce
from . import (
    DO_NOT_CACHE,
    EXPIRE_IMMEDIATELY,
    NEVER_EXPIRE,
    CacheDirectives,
    ExpirationTime,
    get_expiration_datetime,
    get_expiration_seconds,
    get_url_expiration,
)
from .settings import CacheSettings

if TYPE_CHECKING:
    from ..models import CachedResponse

logger = getLogger(__name__)


# TODO: Add custom __rich_repr__ to exclude default values to make logs cleaner (w/ RichHandler)
@define
class CacheActions:
    """Translates cache settings and headers into specific actions to take for a given cache item.
     The resulting actions are then handled in :py:meth:`CachedSession.send`.

    .. rubric:: Notes

    * See :ref:`precedence` for behavior if multiple sources provide an expiration
    * See :ref:`headers` for more details about header behavior
    * The following arguments/properties are the outputs of this class:

    Args:
        cache_key: The cache key created based on the initial request
        error_504: Indicates the request cannot be fulfilled based on cache settings
        expire_after: User or header-provided expiration value
        send_request: Send a new request
        resend_request: Send a new request to refresh a stale cache item
        skip_read: Skip reading from the cache
        skip_write: Skip writing to the cache
    """

    # Outputs
    cache_key: str = field(default=None)
    error_504: bool = field(default=False)
    expire_after: ExpirationTime = field(default=None)
    resend_request: bool = field(default=False)
    send_request: bool = field(default=False)
    skip_read: bool = field(default=False)
    skip_write: bool = field(default=False)

    # Inputs
    _directives: CacheDirectives = field(default=None, repr=False)
    _settings: CacheSettings = field(default=None, repr=False)

    # Temporary attributes
    _only_if_cached: bool = field(default=False, repr=False)
    _refresh: bool = field(default=False, repr=False)
    _stale_if_error: Union[bool, ExpirationTime] = field(default=None, repr=False)
    _validation_headers: Dict[str, str] = field(factory=dict, repr=False)

    @classmethod
    def from_request(cls, cache_key: str, request: PreparedRequest, settings: CacheSettings = None):
        """Initialize from request info and cache settings.

        Note on refreshing: `must-revalidate` isn't a standard request header, but is used here to
        indicate a user-requested refresh. Typically that's only used in response headers, and
        `max-age=0` would be used by a client to request a refresh. However, this would conflict
        with the `expire_after` option provided in :py:meth:`.CachedSession.request`.
        """
        settings = settings or CacheSettings()
        directives = CacheDirectives.from_headers(request.headers)
        logger.debug(f'Cache directives from request headers: {directives}')

        # Merge values that may come from either settings or headers
        only_if_cached = settings.only_if_cached or directives.only_if_cached
        refresh = directives.max_age == EXPIRE_IMMEDIATELY or directives.must_revalidate
        stale_if_error = settings.stale_if_error or directives.stale_if_error

        # Check expiration values in order of precedence
        expire_after = coalesce(
            directives.max_age,
            get_url_expiration(request.url, settings.urls_expire_after),
            settings.expire_after,
        )

        # Check and log conditions for reading from the cache
        read_criteria = {
            'disabled cache': settings.disabled,
            'disabled method': str(request.method) not in settings.allowable_methods,
            'disabled by headers or refresh': directives.no_cache or directives.no_store,
            'disabled by expiration': expire_after == DO_NOT_CACHE,
        }
        _log_cache_criteria('read', read_criteria)

        actions = cls(
            cache_key=cache_key,
            expire_after=expire_after,
            only_if_cached=only_if_cached,
            refresh=refresh,
            skip_read=any(read_criteria.values()),
            skip_write=directives.no_store,
            stale_if_error=stale_if_error,
            directives=directives,
            settings=settings,
        )
        return actions

    @property
    def expires(self) -> Optional[datetime]:
        """Convert the user/header-provided expiration value to a datetime. Applies to new cached
        responses, and previously cached responses that are being revalidated.
        """
        return get_expiration_datetime(self.expire_after)

    # TODO: Better name?
    def is_usable(self, cached_response: 'CachedResponse', error: bool = False):
        """Determine whether a given cached response is "fresh enough" to satisfy the request,
        based on min-fresh, max-stale, or stale-if-error (if an error has occured).
        """
        if cached_response is None:
            return False
        elif cached_response.expires is None:
            return True
        # Handle additional types supported for stale_if_error
        elif error and self._stale_if_error is True:
            return True
        elif error and self._stale_if_error:
            offset_seconds = get_expiration_seconds(self._stale_if_error)
            offset = timedelta(seconds=offset_seconds)
        # Handle min-fresh and max-stale
        else:
            offset = self._directives.get_expire_offset()

        return datetime.utcnow() < cached_response.expires + offset

    def update_from_cached_response(self, cached_response: 'CachedResponse'):
        """Determine if we can reuse a cached response, or set headers for a conditional request
        if possible.

        Used after fetching a cached response, but before potentially sending a new request.
        """
        valid_response = self.is_usable(cached_response)
        valid_if_error = self.is_usable(cached_response, error=True)

        # Can't satisfy the request
        if not valid_response and self._only_if_cached and not valid_if_error:
            self.error_504 = True
        # Send the request for the first time
        elif cached_response is None:
            self.send_request = True
        # Resend the request, unless settings permit a stale response
        elif not valid_response and not (self._only_if_cached and valid_if_error):
            self.resend_request = True

        if cached_response is not None:
            self._update_validation_headers(cached_response)
        logger.debug(f'Post-read cache actions: {self}')

    def update_from_response(self, response: Response):
        """Update expiration + actions based on headers and other details from a new response.

        Used after receiving a new response, but before saving it to the cache.
        """
        directives = CacheDirectives.from_headers(response.headers)
        if self._settings.cache_control:
            self._update_from_response_headers(directives)

        # If "expired" but there's a validator, save it to the cache and revalidate on use
        do_not_cache = self.expire_after == DO_NOT_CACHE
        skip_stale = self.expire_after == EXPIRE_IMMEDIATELY and not directives.has_validator

        # Apply filter callback, if any
        callback = self._settings.filter_fn
        filtered_out = callback is not None and not callback(response)

        # Check and log conditions for writing to the cache
        write_criteria = {
            'disabled cache': self._settings.disabled,
            'disabled method': str(response.request.method) not in self._settings.allowable_methods,
            'disabled status': response.status_code not in self._settings.allowable_codes,
            'disabled by filter': filtered_out,
            'disabled by headers': self.skip_write,
            'disabled by expiration': do_not_cache or skip_stale,
        }
        self.skip_write = any(write_criteria.values())
        _log_cache_criteria('write', write_criteria)

    def update_request(self, request: PreparedRequest) -> PreparedRequest:
        """Apply validation headers (if any) before sending a request"""
        request.headers.update(self._validation_headers)
        return request

    def update_revalidated_response(
        self, response: Response, cached_response: 'CachedResponse'
    ) -> 'CachedResponse':
        """After revalidation, update the cached response's expiration and headers"""
        logger.debug(f'Response for URL {response.request.url} has not been modified')
        cached_response.expires = self.expires
        cached_response.headers.update(response.headers)
        return cached_response

    def _update_from_response_headers(self, directives: CacheDirectives):
        """Check response headers for expiration and other cache directives"""
        logger.debug(f'Cache directives from response headers: {directives}')

        self._stale_if_error = self._stale_if_error or directives.stale_if_error
        if directives.immutable:
            self.expire_after = NEVER_EXPIRE
        else:
            self.expire_after = coalesce(
                directives.max_age,
                directives.expires,
                self.expire_after,
            )
        self.skip_write = self.skip_write or directives.no_store

    def _update_validation_headers(self, cached_response: 'CachedResponse'):
        """If needed, get validation headers based on a cached response. Revalidation may be
        triggered by a stale response, request headers, or cached response headers.
        """
        directives = CacheDirectives.from_headers(cached_response.headers)
        revalidate = directives.has_validator and (
            cached_response.is_expired
            or self._refresh
            or directives.no_cache
            or directives.must_revalidate
            and directives.max_age == 0
        )

        # Add the appropriate validation headers, if needed
        if revalidate:
            if directives.etag:
                self._validation_headers['If-None-Match'] = directives.etag
            if directives.last_modified:
                self._validation_headers['If-Modified-Since'] = directives.last_modified
            self.send_request = True
            self.resend_request = False


def _log_cache_criteria(operation: str, criteria: Dict):
    """Log details on any failed checks for cache read or write"""
    if any(criteria.values()):
        status = ', '.join([k for k, v in criteria.items() if v])
    else:
        status = 'Passed'
    logger.debug(f'Pre-{operation} cache checks: {status}')
