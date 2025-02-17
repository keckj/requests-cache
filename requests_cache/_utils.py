"""Minor internal utility functions that don't really belong anywhere else"""
from inspect import signature
from logging import getLogger
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

KwargDict = Dict[str, Any]
logger = getLogger('requests_cache')


def chunkify(iterable: Iterable, max_size: int) -> Iterator[List]:
    """Split an iterable into chunks of a max size"""
    iterable = list(iterable)
    for index in range(0, len(iterable), max_size):
        yield iterable[index : index + max_size]


def coalesce(*values: Any, default=None) -> Any:
    """Get the first non-``None`` value in a list of values"""
    return next((v for v in values if v is not None), default)


def decode(value, encoding='utf-8') -> str:
    """Decode a value from bytes, if hasn't already been.
    Note: ``PreparedRequest.body`` is always encoded in utf-8.
    """
    return value.decode(encoding) if isinstance(value, bytes) else value


def encode(value, encoding='utf-8') -> bytes:
    """Encode a value to bytes, if it hasn't already been"""
    return value if isinstance(value, bytes) else str(value).encode(encoding)


def get_placeholder_class(original_exception: Exception = None):
    """Create a placeholder type for a class that does not have dependencies installed.
    This allows delaying ImportErrors until init time, rather than at import time.
    """

    def _log_error():
        msg = 'Dependencies are not installed for this feature'
        logger.error(msg)
        raise original_exception or ImportError(msg)

    class Placeholder:
        def __init__(self, *args, **kwargs):
            _log_error()

        def __getattr__(self, *args, **kwargs):
            _log_error()

        def dumps(self, *args, **kwargs):
            _log_error()

    return Placeholder


def get_valid_init_kwargs(obj, kwargs):
    kwds = {}
    for cls in set(type(obj).__mro__).difference({type(obj)}):
        kwds.update(get_valid_kwargs(cls.__init__, kwargs))
    return kwds


def get_valid_kwargs(func: Callable, kwargs: Dict, extras: Iterable[str] = None) -> KwargDict:
    """Get the subset of non-None ``kwargs`` that are valid arguments for ``func``"""
    kwargs, _ = split_kwargs(func, kwargs, extras)
    return {k: v for k, v in kwargs.items() if v is not None}


def split_kwargs(
    func: Callable, kwargs: Dict, extras: Iterable[str] = None
) -> Tuple[KwargDict, KwargDict]:
    """Split ``kwargs`` into two dicts: those that are valid arguments for ``func``,  and those that
    are not
    """
    params = list(signature(func).parameters)
    params.extend(extras or [])
    valid_kwargs = {k: v for k, v in kwargs.items() if k in params}
    invalid_kwargs = {k: v for k, v in kwargs.items() if k not in params}
    return valid_kwargs, invalid_kwargs


def try_int(value: Any) -> Optional[int]:
    """Convert a value to an int, if possible, otherwise ``None``"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
