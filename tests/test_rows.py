"""Tests for row normalization utilities."""

from __future__ import annotations

from dataclasses import dataclass

from wxpath.core.ops import WxStr

from wsql.core.models import DataIntentWithProjection, ProjectionMetadata
from wsql.util.rows import coerce_row_value, row_to_dict


@dataclass
class _TextNode:
    value: str


class _XPathMapLike:
    """Minimal stand-in for elementpath XPathMap."""

    def __init__(self, mapping: dict[str, object]) -> None:
        self._map = mapping


def test_row_to_dict_passthrough_plain_dict() -> None:
    """Plain dict rows are returned with coerced values."""
    row = row_to_dict({'title': 'Example', 'count': 2})
    assert row == {'title': 'Example', 'count': 2}


def test_row_to_dict_returns_none_for_error_dict() -> None:
    """Error payloads are not treated as result rows."""
    assert row_to_dict({'__type__': 'error', 'reason': 'network_error'}) is None


def test_row_to_dict_unwraps_xpath_map_like_object() -> None:
    """XPathMap-like objects are converted via ``_map``."""
    row = row_to_dict(_XPathMapLike({'title': 'Hello', 'author': 'Ada'}))
    assert row == {'title': 'Hello', 'author': 'Ada'}


def test_row_to_dict_unwraps_data_intent_with_projection() -> None:
    """DataIntentWithProjection wrappers are unwrapped."""
    payload = _XPathMapLike({'title': 'Wrapped'})
    intent = DataIntentWithProjection(
        value=payload,
        projection_metadata=ProjectionMetadata(expected_projections=[]),
    )
    assert row_to_dict(intent) == {'title': 'Wrapped'}


def test_coerce_row_value_handles_wxstr_and_textnode() -> None:
    """WxStr and TextNode values coerce to plain strings."""
    wx = WxStr('https://example.com', base_url='https://example.com', depth=0)
    assert coerce_row_value(wx) == 'https://example.com'
    assert coerce_row_value(_TextNode('bio text')) == 'bio text'


def test_coerce_row_value_handles_nested_lists_and_maps() -> None:
    """Nested lists and map-like values coerce recursively."""
    value = {
        'tags': ['a', 'b'],
        'meta': _XPathMapLike({'count': 3}),
    }
    assert coerce_row_value(value) == {'tags': ['a', 'b'], 'meta': {'count': 3}}


def test_row_to_dict_returns_none_for_unsupported_output() -> None:
    """Unsupported engine payloads return ``None``."""
    assert row_to_dict(42) is None
