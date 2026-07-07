"""Row normalization utilities for WSQL execution results."""

from __future__ import annotations

from typing import Any

from wsql.core.models import DataIntentWithProjection

_SCALAR_TYPES = (str, int, float, bool, type(None))


def coerce_row_value(value: Any) -> Any:
    """Coerce a wxpath/elementpath value into a plain Python value.

    Args:
        value: Engine cell value (scalar, list, XPath map, or wrapper type).

    Returns:
        JSON-friendly Python value.
    """
    if value is None or isinstance(value, _SCALAR_TYPES):
        return value

    if isinstance(value, list):
        return [coerce_row_value(item) for item in value]

    if isinstance(value, dict):
        return {str(key): coerce_row_value(item) for key, item in value.items()}

    row_map = getattr(value, '_map', None)
    if row_map is not None:
        return {str(key): coerce_row_value(item) for key, item in row_map.items()}

    unwrapped = _unwrap_value_attr(value)
    if unwrapped is not None:
        return coerce_row_value(unwrapped)

    if isinstance(value, DataIntentWithProjection):
        return coerce_row_value(value.value)

    return str(value)


def _unwrap_value_attr(value: Any) -> Any | None:
    """Return ``value.value`` for simple wrapper nodes when safe."""
    if isinstance(value, (dict, list, tuple, str, bytes, int, float, bool)):
        return None
    if getattr(value, '_map', None) is not None:
        return None
    if not hasattr(value, 'value'):
        return None
    unwrapped = value.value
    if unwrapped is value:
        return None
    return unwrapped


def row_to_dict(output: Any) -> dict[str, Any] | None:
    """Convert wxpath engine output into a plain result row.

    Handles ``DataIntentWithProjection``, elementpath ``XPathMap`` objects
    (``_map`` attribute), and plain dictionaries from test doubles.

    Args:
        output: A single value yielded by the wxpath engine.

    Returns:
        Column mapping for a result row, or ``None`` when *output* is an
        error dictionary or unsupported payload.
    """
    if isinstance(output, dict):
        if output.get('__type__') == 'error':
            return None
        return {str(key): coerce_row_value(value) for key, value in output.items()}

    if isinstance(output, DataIntentWithProjection):
        return row_to_dict(output.value)

    row_map = getattr(output, '_map', None)
    if row_map is not None:
        return {str(key): coerce_row_value(value) for key, value in row_map.items()}

    return None
