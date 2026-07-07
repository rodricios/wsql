"""Tests for WSQL operators (ops.py).

Uses lightweight mock elements to avoid a live wxpath/HTTP dependency
while verifying that operators yield the correct intents.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from wxpath.core.models import DataIntent
from wxpath.core.parser import Segments

from wsql.core.models import SelectExtract
from wsql.core.ops import (
    _evaluate_columns,
    _handle_select_extract,
    _template_url,
)

# --------------------------------------------------------------------
#  Mock element
# --------------------------------------------------------------------


class MockElement:
    """Minimal stand-in for an lxml ``HtmlElement`` with ``xpath3``.

    Associates XPath expressions with canned return values so that
    operator tests can run without a live DOM.

    Args:
        data: Mapping of XPath expression -> list of return values.
        base_url: Simulated ``base_url`` attribute.
    """

    def __init__(
        self,
        data: dict[str, list[Any]] | None = None,
        base_url: str = 'https://example.com',
    ) -> None:
        self._data: dict[str, list[Any]] = data or {}
        self.base_url: str = base_url
        self.tag: str = 'html'

    def xpath3(self, expr: str) -> list:
        """Return the canned value list for *expr*, or ``[]``."""
        return self._data.get(expr, [])


# --------------------------------------------------------------------
#  _evaluate_columns helper
# --------------------------------------------------------------------


class TestEvaluateColumns:
    """Unit tests for the shared ``_evaluate_columns`` helper."""

    def test_single_value(self) -> None:
        """Single-element result stored directly (not as list)."""
        elem = MockElement({'//h1/text()': ['Hello World']})
        result = _evaluate_columns(
            elem,
            columns=[('title', '//h1/text()')],
            base_url='https://example.com',
            depth=0,
        )
        assert result == {'title': 'Hello World'}

    def test_multiple_values(self) -> None:
        """Multi-element result stored as a list."""
        elem = MockElement({'//p/text()': ['para1', 'para2']})
        result = _evaluate_columns(
            elem,
            columns=[('paragraphs', '//p/text()')],
            base_url='https://example.com',
            depth=0,
        )
        assert result['paragraphs'] == ['para1', 'para2']

    def test_empty_result(self) -> None:
        """Missing expression returns ``None``."""
        elem = MockElement({})
        result = _evaluate_columns(
            elem,
            columns=[('missing', '//nonexistent')],
            base_url=None,
            depth=0,
        )
        assert result == {'missing': None}

    def test_non_string_value(self) -> None:
        """Non-string values (e.g. numbers) pass through unwrapped."""
        elem = MockElement({'count(//p)': [42]})
        result = _evaluate_columns(
            elem,
            columns=[('count', 'count(//p)')],
            base_url=None,
            depth=0,
        )
        assert result == {'count': 42}

    def test_xpath_exception_treated_as_empty(self) -> None:
        """If xpath3 raises, the column is treated as empty (None)."""
        elem = MockElement({})
        # Patch xpath3 to raise
        elem.xpath3 = MagicMock(side_effect=RuntimeError('boom'))

        result = _evaluate_columns(
            elem,
            columns=[('bad', '//oops')],
            base_url=None,
            depth=0,
        )
        assert result == {'bad': None}


# --------------------------------------------------------------------
#  _template_url helper
# --------------------------------------------------------------------


class TestTemplateURL:
    """URL template substitution."""

    def test_basic_substitution(self) -> None:
        """Single placeholder replaced with URL-encoded value."""
        url = _template_url(
            'https://en.wikipedia.org/wiki/{authors.name}',
            'authors',
            {'name': 'Albert Einstein'},
        )
        assert url == 'https://en.wikipedia.org/wiki/Albert%20Einstein'

    def test_multiple_placeholders(self) -> None:
        """Multiple placeholders in the same template."""
        url = _template_url(
            'https://example.com/{d.a}/{d.b}',
            'd',
            {'a': 'hello', 'b': 'world'},
        )
        assert url == 'https://example.com/hello/world'

    def test_special_chars_encoded(self) -> None:
        """Characters like & and ? are percent-encoded."""
        url = _template_url(
            'https://example.com/search/{d.q}',
            'd',
            {'q': 'a&b=c'},
        )
        assert '%26' in url
        assert '%3D' in url

    def test_no_placeholder(self) -> None:
        """Template without any placeholders is returned unchanged."""
        url = _template_url('https://example.com', 'd', {'x': '1'})
        assert url == 'https://example.com'


# --------------------------------------------------------------------
#  SelectExtract operator
# --------------------------------------------------------------------


class TestSelectExtractOp:
    """Tests for the ``_handle_select_extract`` operator."""

    def test_yields_data_intent(self) -> None:
        """Operator yields exactly one DataIntent with a dict value."""
        elem = MockElement(
            {
                '//h1/text()': ['Title'],
                '//p/text()': ['Body'],
            }
        )
        extract = SelectExtract(columns=[('title', '//h1/text()'), ('body', '//p/text()')])
        segments = Segments([extract])

        intents = list(_handle_select_extract(elem, segments, curr_depth=0))

        assert len(intents) == 1
        assert isinstance(intents[0], DataIntent)
        assert intents[0].value == {'title': 'Title', 'body': 'Body'}

    def test_empty_columns(self) -> None:
        """No columns -> empty dict."""
        elem = MockElement({})
        extract = SelectExtract(columns=[])
        segments = Segments([extract])

        intents = list(_handle_select_extract(elem, segments, curr_depth=0))

        assert intents[0].value == {}
