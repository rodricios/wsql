"""WSQL operators - SELECT extraction and JOIN semantics for the wxpath engine.

Extends wxpath's operator registry with five handlers:

    ``RowIterate``
        Evaluates an anchor XPath against the current element and
        yields one ``ProcessIntent`` per matched node (PER clause).

    ``UrlFromXpath``
        Evaluates an XPath to extract URLs, then yields a
        ``CrawlIntent`` for each (FROM subquery pattern).

    ``SelectExtract``
        Evaluates ``(alias, xpath_expr)`` pairs and yields a
        ``DataIntent`` dict.

    ``MergeMap``
        Like ``SelectExtract`` but merges pre-collected CTE row data
        (JOIN semantics).

    ``JoinSeed`` (``'wsql:join_seed'``)
        Iterates over CTE results, templates URL per row, yields
        ``CrawlIntent`` with ``MergeMap`` in next_segments.

Registration happens at import time via wxpath's ``OPS_REGISTER``.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import quote

from lxml import html
from wxpath.core.dom import get_absolute_links_from_elem_and_xpath
from wxpath.core.models import CrawlIntent, DataIntent, ProcessIntent
from wxpath.core.ops import WxStr, _handle_xpath, register
from wxpath.core.parser import Depth, Segments, String

from wsql.core.models import (
    CrawlIntentWithProjection,
    DataIntentWithProjection,
    RowIterate,
    SelectExtract,
    UrlCrawlWithProjection,
    UrlFromXpath,
    XPathWithProjection,
)


# TODO: I'm preferring to register the function by Class object instead of string
#       i.e. @register(UrlCrawlWithProjection)
@register('url_projection', (String,))
@register('url_projection', (String, Depth))
def _handle_url_crawl_with_projection(
    curr_elem: html.HtmlElement,
    curr_segments: Segments,
    curr_depth: int,
    **kwargs,
) -> Iterable[CrawlIntent]:
    """Handle URL crawl with projection."""
    url_crawl_node: UrlCrawlWithProjection = curr_segments[0]
    if url_crawl_node.projection_metadata.projected_values:
        url = url_crawl_node.args[0].value
        for key, value in url_crawl_node.projection_metadata.projected_values.items():
            if value.__class__.__name__ == 'TextNode':
                value = value.value
            else:
                value = str(value)
            url = url.replace(f'{{{key}}}', value)
        url_crawl_node.args[0].value = url
    yield CrawlIntentWithProjection(
        url=url_crawl_node.args[0].value,
        next_segments=curr_segments[1:],
        projection_metadata=url_crawl_node.projection_metadata,
    )


@register(XPathWithProjection)
def _handle_xpath_with_projection(
    curr_elem: html.HtmlElement,
    curr_segments: Segments,
    curr_depth: int,
    **kwargs,
) -> Iterable[DataIntent]:
    """Handle XPath with projection."""
    xpath_node: XPathWithProjection = curr_segments[0]

    for value in _handle_xpath(curr_elem, curr_segments, curr_depth):
        assert isinstance(value, DataIntent), 'XPath with projection must yield a DataIntent'
        yield DataIntentWithProjection(
            value=value.value,
            projection_metadata=xpath_node.projection_metadata,
        )


# --------------------------------------------------------------------
#  Shared helpers
# --------------------------------------------------------------------


def _evaluate_columns(
    elem: html.HtmlElement,
    columns: list[tuple[str, str]],
    base_url: str | None,
    depth: int,
) -> dict:
    """Evaluate a sequence of ``(alias, xpath_expr)`` pairs.

    Each XPath is evaluated against *elem*. Results are coerced:
    single value -> stored directly; empty -> None; multiple -> list.
    """
    result: dict = {}

    for alias, expr in columns:
        try:
            values = elem.xpath3(expr)
        except Exception:
            values = []

        if isinstance(values, list):
            if len(values) == 1:
                v = values[0]
                result[alias] = (
                    WxStr(v, base_url=base_url, depth=depth) if isinstance(v, str) else v
                )
            elif not values:
                result[alias] = None
            else:
                result[alias] = [
                    WxStr(v, base_url=base_url, depth=depth) if isinstance(v, str) else v
                    for v in values
                ]
        else:
            result[alias] = (
                WxStr(values, base_url=base_url, depth=depth) if isinstance(values, str) else values
            )

    return result


def _template_url(template: str, cte_name: str, row: dict) -> str:
    """Substitute ``{cte_name.field}`` placeholders; values are percent-encoded."""
    url = template
    for field_name, value in row.items():
        placeholder = f'{{{cte_name}.{field_name}}}'
        encoded_value = quote(str(value), safe='')
        url = url.replace(placeholder, encoded_value)
    return url


# --------------------------------------------------------------------
#  RowIterate  -  fan-out ProcessIntents for PER anchor nodes
# --------------------------------------------------------------------


@register(RowIterate)
def _handle_row_iterate(
    curr_elem: html.HtmlElement,
    curr_segments: Segments,
    curr_depth: int,
    **kwargs,
) -> Iterable[ProcessIntent]:
    """Iterate over anchor nodes and chain each to downstream segments."""
    row_node: RowIterate = curr_segments[0]
    base_url = getattr(curr_elem, 'base_url', None)

    try:
        anchors = curr_elem.xpath3(row_node.anchor_xpath)
    except Exception:
        anchors = []

    next_segments = Segments(row_node.next_segments)

    for anchor in anchors:
        if base_url is not None:
            try:
                anchor.base_url = base_url
            except AttributeError:
                pass
        yield ProcessIntent(elem=anchor, next_segments=next_segments)


# --------------------------------------------------------------------
#  UrlFromXpath  -  extract URLs via XPath and queue fetches
# --------------------------------------------------------------------


@register(UrlFromXpath)
def _handle_url_from_xpath(
    curr_elem: html.HtmlElement,
    curr_segments: Segments,
    curr_depth: int,
    **kwargs,
) -> Iterable[CrawlIntent]:
    """Evaluate XPath to extract URLs and queue each for fetching."""
    url_node: UrlFromXpath = curr_segments[0]
    urls = get_absolute_links_from_elem_and_xpath(curr_elem, url_node.url_xpath)
    urls = list(dict.fromkeys(urls))
    next_segments = Segments(url_node.next_segments)
    for url in urls:
        yield CrawlIntent(url=url, next_segments=next_segments)


# --------------------------------------------------------------------
#  SelectExtract  -  evaluate SELECT columns -> dict
# --------------------------------------------------------------------


@register(SelectExtract)
def _handle_select_extract(
    curr_elem: html.HtmlElement,
    curr_segments: Segments,
    curr_depth: int,
    **kwargs,
) -> Iterable[DataIntent]:
    """Evaluate every SELECT column against the current element."""
    select_node: SelectExtract = curr_segments[0]
    base_url = getattr(curr_elem, 'base_url', None)
    result = _evaluate_columns(
        elem=curr_elem,
        columns=select_node.columns,
        base_url=base_url,
        depth=curr_depth,
    )
    yield DataIntent(value=result)
