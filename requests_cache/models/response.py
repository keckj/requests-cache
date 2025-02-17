from __future__ import annotations

from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import TYPE_CHECKING, List, Optional

import attr
from attr import define, field
from requests import PreparedRequest, Response
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict
from urllib3._collections import HTTPHeaderDict

from ..policy.expiration import ExpirationTime, get_expiration_datetime
from . import CachedHTTPResponse, CachedRequest

if TYPE_CHECKING:
    from ..policy.actions import CacheActions

DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S %Z'  # Format used for __str__ only
logger = getLogger(__name__)


@define(auto_attribs=False, slots=False)
class BaseResponse(Response):
    """Wrapper class for responses returned by :py:class:`.CachedSession`. This mainly exists to
    provide type hints for extra cache-related attributes that are added to non-cached responses.
    """

    cache_key: Optional[str] = None
    created_at: datetime = field(factory=datetime.utcnow)
    expires: Optional[datetime] = field(default=None)

    @property
    def from_cache(self) -> bool:
        return False

    @property
    def is_expired(self) -> bool:
        return False


@define(auto_attribs=False, repr=False, slots=False)
class OriginalResponse(BaseResponse):
    """Wrapper class for non-cached responses returned by :py:class:`.CachedSession`"""

    @classmethod
    def wrap_response(cls, response: Response, actions: 'CacheActions'):
        """Modify a response object in-place and add extra cache-related attributes"""
        if not isinstance(response, cls):
            response.__class__ = cls
            # Add expires and cache_key only if the response was written to the cache
            response.expires = None if actions.skip_write else actions.expires  # type: ignore
            response.cache_key = None if actions.skip_write else actions.cache_key  # type: ignore
            response.created_at = datetime.utcnow()  # type: ignore
        return response


@define(auto_attribs=False, slots=False)
class CachedResponse(BaseResponse):
    """A class that emulates :py:class:`requests.Response`, optimized for serialization"""

    _content: bytes = field(default=None)
    _next: Optional[CachedRequest] = field(default=None)
    cache_key: Optional[str] = None  # Not serialized; set by BaseCache.get_response()
    cookies: RequestsCookieJar = field(factory=RequestsCookieJar)
    created_at: datetime = field(factory=datetime.utcnow)
    elapsed: timedelta = field(factory=timedelta)
    encoding: str = field(default=None)
    expires: Optional[datetime] = field(default=None)
    headers: CaseInsensitiveDict = field(factory=CaseInsensitiveDict)
    history: List['CachedResponse'] = field(factory=list)  # type: ignore
    raw: CachedHTTPResponse = field(factory=CachedHTTPResponse, repr=False)
    reason: str = field(default=None)
    request: CachedRequest = field(factory=CachedRequest)  # type: ignore
    status_code: int = field(default=0)
    url: str = field(default=None)

    def __attrs_post_init__(self):
        """Re-initialize raw response body after deserialization"""
        if self.raw._body is None and self._content is not None:
            self.raw.reset(self._content)
        if not self.raw.headers:
            self.raw.headers = HTTPHeaderDict(self.headers)

    @classmethod
    def from_response(cls, response: Response, **kwargs):
        """Create a CachedResponse based on an original Response or another CachedResponse object"""
        if isinstance(response, CachedResponse):
            obj = attr.evolve(response, **kwargs)
            obj._convert_redirects()
            return obj

        obj = cls(**kwargs)

        # Copy basic attributes
        for k in Response.__attrs__:
            setattr(obj, k, getattr(response, k, None))

        # Store request, raw response, and next response (if it's a redirect response)
        obj.request = CachedRequest.from_request(response.request)
        obj.raw = CachedHTTPResponse.from_response(response)
        obj._next = CachedRequest.from_request(response.next) if response.next else None

        # Store response body, which will have been read & decoded by requests.Response by now
        obj._content = response.content

        obj._convert_redirects()
        return obj

    def _convert_redirects(self):
        """Convert redirect history, if any; avoid recursion by not copying redirects of redirects"""
        if self.is_redirect:
            self.history = []
            return
        self.history = [self.from_response(redirect) for redirect in self.history]

    @property
    def _content_consumed(self) -> bool:
        """For compatibility with requests.Response; will always be True for a cached response"""
        return True

    @_content_consumed.setter
    def _content_consumed(self, value: bool):
        pass

    @property
    def from_cache(self) -> bool:
        return True

    @property
    def is_expired(self) -> bool:
        """Determine if this cached response is expired"""
        return self.expires is not None and datetime.utcnow() >= self.expires

    @property
    def ttl(self) -> Optional[int]:
        """Get time to expiration in seconds"""
        if self.expires is None or self.is_expired:
            return None
        delta = self.expires - datetime.utcnow()
        return int(delta.total_seconds())

    @property
    def next(self) -> Optional[PreparedRequest]:
        """Returns a PreparedRequest for the next request in a redirect chain, if there is one."""
        return self._next.prepare() if self._next else None

    def reset_expiration(self, expire_after: ExpirationTime) -> bool:
        """Set a new expiration for this response, and determine if it is now expired"""
        self.expires = get_expiration_datetime(expire_after)
        return self.is_expired

    @property
    def size(self) -> int:
        """Get the size of the response body in bytes"""
        return len(self.content) if self.content else 0

    def __getstate__(self):
        """Override pickling behavior from ``requests.Response.__getstate__``"""
        return self.__dict__

    def __setstate__(self, state):
        """Override pickling behavior from ``requests.Response.__setstate__``"""
        for name, value in state.items():
            setattr(self, name, value)

    def __str__(self):
        return (
            f'<CachedResponse [{self.status_code}]: '
            f'created: {format_datetime(self.created_at)}, '
            f'expires: {format_datetime(self.expires)} ({"stale" if self.is_expired else "fresh"}), '
            f'size: {format_file_size(self.size)}, request: {self.request}>'
        )


def format_datetime(value: Optional[datetime]) -> str:
    """Get a formatted datetime string in the local time zone"""
    if not value:
        return "N/A"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime(DATETIME_FORMAT)


def format_file_size(n_bytes: int) -> str:
    """Convert a file size in bytes into a human-readable format"""
    filesize = float(n_bytes or 0)

    def _format(unit):
        return f'{int(filesize)} {unit}' if unit == 'bytes' else f'{filesize:.2f} {unit}'

    for unit in ['bytes', 'KiB', 'MiB', 'GiB']:
        if filesize < 1024 or unit == 'GiB':
            return _format(unit)
        filesize /= 1024

    if TYPE_CHECKING:
        return _format(unit)
