"""WSQL data models - Intermediate AST and wxpath-compatible segment types.

This module defines two tiers of data structures:

    1. **Intermediate AST** - produced by the WSQL parser, representing
       the SQL-like syntax tree (SelectColumn, FromClause, JoinClause,
       WSQLQuery, CTE, WSQLProgram).

    2. **wxpath-compatible segments** - nodes that slot directly into
       wxpath's segment pipeline (SelectExtract, MergeMap, JoinSeed).

The intermediate AST is transpiled into wxpath segments by the functions
in ``wsql.core.parser``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from wxpath.core.models import CrawlIntent, DataIntent
from wxpath.core.parser import UrlCrawl, Xpath

# --------------------------------------------------------------------
#  Intermediate AST  -  produced by the recursive-descent parser
# --------------------------------------------------------------------

_CTE_REF_RE = re.compile(r'^([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)$')
"""Pattern for detecting CTE field references: ``cte_name.field``."""

_CTE_REF_FROM_URL_RE = re.compile(r'\{([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\}')
"""Pattern for detecting CTE field references in URL templates: ``{cte_name.field}``."""


@dataclass
class CrawlIntentWithProjection(CrawlIntent):
    projection_metadata: ProjectionMetadata


@dataclass
class DataIntentWithProjection(DataIntent):
    projection_metadata: ProjectionMetadata

    def get_value(self) -> Any:
        return self


@dataclass
class ProjectionMetadata:
    """A mixin for WSQL operations. Will be used to project values from query dependencies.

    Attributes:
        expected_projections: The expected projections of the WSQL operation.
        projected_values: The projected values of the WSQL operation.
        parent_query_name: The name of the parent query this operation is a part of.
        parent_query_id: The id of the parent query this operation is a part of.
    """

    expected_projections: list[ParentQueryFieldRef]
    projected_values: dict[str, Any] = field(default_factory=dict)
    parent_query_name: str | None = None
    parent_query_id: int | None = None

    def dependencies(self) -> list[str]:
        """Get the dependencies of the WSQL operation."""
        return [projection.cte_name for projection in self.expected_projections]


class ParentQueryFieldRef(NamedTuple):
    """A projection of an XPath expression.

    Attributes:
        cte_name: The name of the CTE.
        cte_field: The field of the CTE.
    """

    cte_name: str | None = None
    cte_field: str | None = None

    def key(self) -> str:
        return f'{self.cte_name}.{self.cte_field}'


@dataclass
class XPathWithProjection(Xpath):
    projection_metadata: ProjectionMetadata = field(default_factory=ProjectionMetadata)


@dataclass
class UrlCrawlWithProjection(UrlCrawl):
    projection_metadata: ProjectionMetadata = field(default_factory=ProjectionMetadata)


@dataclass
class SelectColumn:
    """A single column in a WSQL ``SELECT`` clause.

    Attributes:
        expr: The raw extraction expression (XPath string or CTE
            reference such as ``author_bios.name``).
        alias: Output key name for this column.
        is_cte_ref: ``True`` when this column references a CTE field
            rather than a live XPath extraction.
        cte_name: Name of the referenced CTE (when ``is_cte_ref`` is True).
        cte_field: Field name within the referenced CTE.
    """

    expr: str
    alias: str
    is_cte_ref: bool = False
    cte_name: str | None = None
    cte_field: str | None = None


@dataclass
class FromClause:
    """The ``FROM`` clause of a WSQL query.

    Supports two source types:

        1. **URL source** - a literal URL or template (e.g.
           ``https://example.com`` or ``https://wiki/{cte.field}``).
        2. **Subquery source** - a nested ``WSQLQuery`` whose single
           ``SELECT`` column produces URLs to fetch.

    Attributes:
        url: A literal URL, URL template, or ``None`` when *subquery*
            is specified instead.
        subquery: An optional nested ``WSQLQuery`` that produces URLs.
        per: Optional XPath expression defining the row anchor.
        follow: Optional XPath expression identifying links to follow.
        depth: Maximum crawl depth (default ``1``).
        cte_dependencies: List of CTE names that this FROM clause depends on.
    """

    url: str | None = None
    subquery: WSQLQuery | None = None
    per: str | None = None
    follow: str | None = None
    depth: int = 1
    crawler_name: str | None = None
    cte_dependencies: list[str] = field(default_factory=list)
    expected_projections: list[ParentQueryFieldRef] = field(default_factory=list)


@dataclass
class JoinClause:
    """A ``JOIN`` clause linking a CTE to the main query.

    Attributes:
        join_type: Join kind - ``INNER``, ``LEFT``, ``RIGHT``, etc.
        cte_name: Identifier of the CTE being joined.
        on_field: Fully-qualified reference for the join predicate
            (e.g. ``author_bios.name``).
    """

    join_type: str
    cte_name: str
    on_field: str


@dataclass
class WSQLQuery:
    """A single WSQL query - either a CTE body or the main statement.

    Attributes:
        select: Ordered list of ``SELECT`` columns.
        from_clause: The ``FROM`` specification.
        joins: Zero or more ``JOIN`` clauses (empty for simple queries).
        alias: Optional alias for the query (used for subqueries).
    """

    select: list[SelectColumn]
    from_clause: FromClause
    joins: list[JoinClause] = field(default_factory=list)
    alias: str | None = None


@dataclass
class CTE:
    """A *Common Table Expression* defined with ``WITH ... AS (...)``.

    Attributes:
        name: The CTE identifier.
        query: The inner ``WSQLQuery``.
    """

    name: str
    query: WSQLQuery


@dataclass
class WSQLProgram:
    """Top-level WSQL program: optional CTEs followed by a main query.

    Attributes:
        ctes: Zero or more CTEs preceding the main query.
        main_query: The primary query to execute.
    """

    ctes: list[CTE]
    main_query: WSQLQuery
    query_name_to_id: dict[str, int] = field(default_factory=dict)
    query_id_to_name: dict[int, str] = field(default_factory=dict)
    query_name_to_query: dict[str, WSQLQuery] = field(default_factory=dict)
    query_id_to_query: dict[int, WSQLQuery] = field(default_factory=dict)

    def has_joins(self) -> bool:
        """Check if the main query has any JOIN clauses."""
        return bool(self.main_query.joins)

    def get_join_cte_names(self) -> list[str]:
        """Get names of all CTEs referenced in JOIN clauses."""
        return [join.cte_name for join in self.main_query.joins]

    def get_all_queries(self) -> list[WSQLQuery]:
        """Get all queries in the program."""
        return [self.main_query] + [cte.query for cte in self.ctes]


# --------------------------------------------------------------------
#  wxpath-compatible segment types  -  consumed by the engine
# --------------------------------------------------------------------


@dataclass
class RowIterate:
    """Iterate over anchor elements and chain to the next segment.

    When the wxpath engine encounters this segment, it evaluates
    *anchor_xpath* against the current element, yielding one
    ``ProcessIntent`` per matched node.

    This implements the ``PER`` clause semantics.

    Attributes:
        anchor_xpath: XPath expression selecting row anchor nodes.
        next_segments: Segment list to execute per anchor.
    """

    anchor_xpath: str
    next_segments: list = field(default_factory=list)


@dataclass
class UrlFromXpath:
    """Evaluate an XPath to extract URLs and queue them for fetching.

    This segment implements the ``FROM (SELECT <xpath> FROM ...)``
    subquery pattern.

    Attributes:
        url_xpath: XPath expression that selects URL strings.
        next_segments: Segment list to execute on each fetched page.
    """

    url_xpath: str
    next_segments: list = field(default_factory=list)


@dataclass
class SelectExtract:
    """Evaluate ``SELECT`` column XPath expressions and yield a dict.

    Each entry in *columns* is an ``(alias, xpath_expr)`` pair.

    Attributes:
        columns: List of ``(alias, xpath_expr)`` pairs.
    """

    columns: list[tuple[str, str]]


@dataclass
class MergeMap(SelectExtract):
    """A ``SelectExtract`` that merges CTE-sourced columns.

    Used inside JOIN pipelines to combine web-extracted data with
    CTE row data.

    Attributes:
        cte_row: The specific CTE result row to merge.
        cte_columns: Pairs of ``(cte_field, output_alias)``.
    """

    cte_row: dict = field(default_factory=dict)
    cte_columns: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class JoinedCrawl:
    """Segment that represents a multi-phase JOIN crawl: roots, optional dependent
    CTE layers, then main-query fan-out per driver row.

    The wxpath-compatible pipeline is split into:

        1. **root_segment_lists** - one segment list per root CTE (no dependencies).
           Executed in parallel. Each list is e.g. [UrlCrawl, Xpath map].
        2. **root_segments** - backward compatibility: first root pipeline when
           there is exactly one root; otherwise same as root_segment_lists[0] if any.
        3. **dependent_cte_layers** - optional. Layer i is a list of (cte_name, segments)
           for CTEs that depend only on roots and layers 0..i-1. Executed in
           topological order (per row of dependencies).
        4. **fan_out_segments** - main query pipeline; executed once per driver
           CTE row, with URL templated from that row.

    Attributes:
        root_segments: First root pipeline (backward compat).
        root_segment_lists: List of root pipelines (one per root CTE).
        dependent_cte_layers: List of layers; each layer is list of
            (cte_name, segment_list) for dependent CTEs.
        fan_out_segments: Main query segment list (template URL + map).
    """

    root_segments: list = field(default_factory=list)
    root_segment_lists: list = field(default_factory=list)
    dependent_cte_layers: list = field(default_factory=list)
    fan_out_segments: list = field(default_factory=list)


@dataclass
class JoinedExtract:
    """Final column mapping for a JOIN: both web-extracted and CTE-sourced columns.

    After JoinedCrawl runs, each row is assembled from web columns
    (evaluated on the fetched page) and CTE columns (from the joined
    CTE row). This segment describes the full output schema.

    Attributes:
        columns: List of ``(alias, expr)`` pairs. *expr* is either an
            XPath expression (for web columns) or a CTE reference
            (e.g. ``author_bios.name``).
    """

    columns: list[tuple[str, str]] = field(default_factory=list)
