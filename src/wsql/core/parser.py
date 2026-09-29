"""WSQL Parser - Recursive-descent parser and wxpath transpiler.

Parses the SQL-like WSQL syntax into an intermediate AST
(``WSQLProgram``), then transpiles it into wxpath-compatible
``Segments`` that the wxpath engine can execute directly.

Architecture
~~~~~~~~~~~~

The parser operates on the raw source string. SQL keywords
(``SELECT``, ``FROM``, ``FOLLOW``, etc.) are recognised at structural
boundaries while XPath expressions between those keywords are captured
verbatim as opaque strings.

This two-level approach avoids re-tokenising XPath and keeps the WSQL
parser focused exclusively on the SQL-like syntax.

Example
~~~~~~~

    >>> program = parse(
    ...     "SELECT //h1/text() AS title "
    ...     "FROM https://example.com DEPTH 1"
    ... )
    >>> segments = transpile(program)
    >>> segments   # wxpath Segments ready for the engine
    Segments([UrlLiteral('url', ...), SelectExtract(...)])
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterator

from wxpath.core.parser import (
    Click,
    Depth,
    Pages,
    Paginate,
    Segments,
    String,
    UrlCrawl,
    UrlLiteral,
    UrlQuery,
    Xpath,
)

from wsql.core.models import (
    _CTE_REF_FROM_URL_RE,
    _CTE_REF_RE,
    CTE,
    FromClause,
    JoinClause,
    ParentQueryFieldRef,
    ProjectionMetadata,
    SelectColumn,
    UrlCrawlWithProjection,
    WSQLProgram,
    WSQLQuery,
    XPathWithProjection,
)

# --------------------------------------------------------------------
#  Recursive-descent parser
# --------------------------------------------------------------------


class WSQLParser:
    """Recursive-descent parser for WSQL.

    Operates directly on the source characters.  SQL keywords are
    recognised at structural boundaries; XPath content between those
    keywords is captured verbatim.

    Grammar (informal)::

        program       -> (WITH ident AS '(' query ')')* query
        query         -> SELECT columns FROM from_clause join_clauses?
        columns       -> column (',' column)*
        column        -> expr (AS ident)?
        from_clause   -> (url | '(' query ')')
                          (PER xpath | FOLLOW xpath | DEPTH int
                           | CLICK xpath | PAGES int | CRAWLER ident)*
        join_clauses  -> (join_type JOIN ident (ON expr))*
        join_type     -> INNER | LEFT | RIGHT | CROSS | FULL

    Args:
        source: The raw WSQL source string.
    """

    # Keywords that may delimit structural sections.
    SECTION_KEYWORDS: frozenset[str] = frozenset(
        {
            'WITH',
            'SELECT',
            'FROM',
            'AS',
            'PER',
            'FOLLOW',
            'DEPTH',
            'CLICK',
            'PAGES',
            'CRAWLER',
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
            'ON',
        }
    )

    # Keywords that terminate a SELECT column expression.
    _COL_STOP_KW: frozenset[str] = frozenset(
        {
            'AS',
            'FROM',
            'SELECT',
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
        }
    )

    # TODO: Restructure this to be more explicit about organizational relationship between
    #       different types of keywords.
    # Keywords that terminate a PER xpath expression.
    _PER_STOP_KW: frozenset[str] = frozenset(
        {
            'FOLLOW',
            'DEPTH',
            'CLICK',
            'PAGES',
            'CRAWLER',
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
        }
    )

    # Keywords that terminate a FOLLOW xpath expression.
    _FOLLOW_STOP_KW: frozenset[str] = frozenset(
        {
            'PER',
            'DEPTH',
            'CLICK',
            'PAGES',
            'CRAWLER',
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
        }
    )

    # Keywords that terminate a CLICK xpath expression.
    _CLICK_STOP_KW: frozenset[str] = frozenset(
        {
            'PER',
            'FOLLOW',
            'DEPTH',
            'PAGES',
            'CRAWLER',
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
        }
    )

    # Keywords that terminate a FROM clause scan.
    _FROM_STOP_KW: frozenset[str] = frozenset(
        {
            'PER',
            'FOLLOW',
            'DEPTH',
            'CLICK',
            'PAGES',
            'CRAWLER',
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
        }
    )

    # Keywords that terminate an ON expression.
    _ON_STOP_KW: frozenset[str] = frozenset(
        {
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
        }
    )

    _CRAWLER_STOP_KW: frozenset[str] = frozenset(
        {
            'PER',
            'FOLLOW',
            'DEPTH',
            'CLICK',
            'PAGES',
            'INNER',
            'LEFT',
            'RIGHT',
            'CROSS',
            'FULL',
            'JOIN',
        }
    )

    def __init__(self, source: str) -> None:
        self.src: str = source
        self.pos: int = 0
        self.end: int = len(source)
        self.query_id_counter: int = 0
        self.query_id_to_name: dict[int, str] = {}
        self.query_name_to_id: dict[str, int] = {}

    # -- low-level helpers ------------------------------------------

    def _skip_ws(self) -> None:
        """Advance past whitespace characters."""
        while self.pos < self.end and self.src[self.pos] in ' \t\n\r':
            self.pos += 1

    def _at_end(self) -> bool:
        """Return *True* when the source has been fully consumed."""
        self._skip_ws()
        return self.pos >= self.end

    def _match_keyword_here(
        self,
        keywords: frozenset[str] | set[str],
    ) -> str | None:
        """Check whether *self.pos* sits at the start of a keyword.

        Matching is **case-insensitive** and respects word boundaries
        (the preceding character must be non-alphanumeric, and the
        character after the keyword must be non-alphanumeric or
        end-of-string).

        Does **not** advance *self.pos*.
        """
        if self.pos > 0 and (
            self.src[self.pos - 1].isalnum()
            or self.src[self.pos - 1] == '_'
            or self.src[self.pos - 1] == '.'
            or self.src[self.pos - 1] == '-'
        ):
            return None

        for kw in keywords:
            kw_end = self.pos + len(kw)
            if kw_end > self.end:
                continue
            if self.src[self.pos : kw_end].upper() == kw:
                if kw_end >= self.end or not (
                    self.src[kw_end].isalnum() or self.src[kw_end] == '_'
                ):
                    return kw
        return None

    def _peek_keyword(self, *keywords: str) -> str | None:
        """Non-consuming lookahead for one of the given keywords."""
        saved = self.pos
        self._skip_ws()
        result = self._match_keyword_here(set(keywords))
        self.pos = saved
        return result

    def _consume_keyword(self, kw: str) -> str:
        """Consume a specific keyword or raise ``SyntaxError``."""
        self._skip_ws()
        if self._match_keyword_here({kw}) is None:
            snippet = self.src[self.pos : self.pos + 30]
            raise SyntaxError(f"Expected '{kw}' at position {self.pos}, found: '{snippet}...'")
        self.pos += len(kw)
        return kw

    def _consume_identifier(self) -> str:
        """Consume an identifier ``[a-zA-Z_][a-zA-Z0-9_]*``."""
        self._skip_ws()
        start = self.pos
        if self.pos < self.end and (self.src[self.pos].isalpha() or self.src[self.pos] == '_'):
            self.pos += 1
            while self.pos < self.end and (
                self.src[self.pos].isalnum() or self.src[self.pos] in '_'
            ):
                self.pos += 1
        if self.pos == start:
            snippet = self.src[self.pos : self.pos + 20]
            raise SyntaxError(f"Expected identifier at position {self.pos}, found: '{snippet}...'")
        return self.src[start : self.pos]

    def _consume_integer(self) -> int:
        """Consume a decimal integer literal."""
        self._skip_ws()
        start = self.pos
        while self.pos < self.end and self.src[self.pos].isdigit():
            self.pos += 1
        if self.pos == start:
            raise SyntaxError(f'Expected integer at position {self.pos}')
        return int(self.src[start : self.pos])

    def _consume_char(self, ch: str) -> None:
        """Consume a single expected character."""
        self._skip_ws()
        if self.pos >= self.end or self.src[self.pos] != ch:
            actual = self.src[self.pos] if self.pos < self.end else 'EOF'
            raise SyntaxError(f"Expected '{ch}' at position {self.pos}, found '{actual}'")
        self.pos += 1

    def _try_char(self, ch: str) -> bool:
        """Try to consume *ch*; return ``True`` on success."""
        self._skip_ws()
        if self.pos < self.end and self.src[self.pos] == ch:
            self.pos += 1
            return True
        return False

    # -- balanced capture -------------------------------------------

    def _capture_balanced(
        self,
        stop_keywords: frozenset[str] = frozenset(),
        stop_chars: str = '',
    ) -> str:
        """Capture source text until a top-level stop condition.

        Respects nesting of ``()``, ``[]``, ``{}``, and quoted strings
        so that SQL keywords inside nested XPath contexts are never
        mistaken for structural delimiters.

        Args:
            stop_keywords: SQL keywords that halt capture at nesting
                depth zero.
            stop_chars: Single characters that halt capture at nesting
                depth zero.

        Returns:
            The captured text with leading/trailing whitespace stripped.
        """
        start = self.pos
        paren = bracket = brace = 0
        in_sq = in_dq = False

        while self.pos < self.end:
            c = self.src[self.pos]

            # toggle quoted-string state
            if c == "'" and not in_dq:
                in_sq = not in_sq
                self.pos += 1
                continue
            if c == '"' and not in_sq:
                in_dq = not in_dq
                self.pos += 1
                continue
            if in_sq or in_dq:
                self.pos += 1
                continue

            # nesting tracking
            if c == '(':
                paren += 1
            elif c == ')':
                if paren == 0:
                    break  # unmatched ) - outer CTE boundary
                paren -= 1
            elif c == '[':
                bracket += 1
            elif c == ']':
                bracket -= 1
            elif c == '{':
                brace += 1
            elif c == '}':
                brace -= 1

            # stop conditions at depth zero
            if paren == 0 and bracket == 0 and brace == 0:
                if stop_chars and c in stop_chars:
                    break
                if c.isalpha() and stop_keywords:
                    kw = self._match_keyword_here(stop_keywords)
                    if kw:
                        break

            self.pos += 1

        return self.src[start : self.pos].strip()

    def _capture_url(self) -> str:
        """Capture a URL - possibly containing ``{cte.field}`` templates.

        Advances until whitespace at brace-depth zero, allowing
        template variables like ``{author_bios.name}`` to span the
        interior of a URL without being broken by their closing ``}``.

        Returns:
            The raw URL string (or URL template).
        """
        self._skip_ws()
        start = self.pos
        brace_depth = 0

        while self.pos < self.end:
            c = self.src[self.pos]
            if c == '{':
                brace_depth += 1
            elif c == '}':
                brace_depth -= 1
            elif c in ' \t\n\r' and brace_depth == 0:
                break
            elif c == ')' and brace_depth == 0:
                # closing paren of a CTE wrapper - do not consume
                break
            self.pos += 1

        return self.src[start : self.pos].strip()

    def _extract_cte_dependencies(self, url: str) -> list[str]:
        """Capture the CTE dependencies from a URL template.

        Args:
            url: The URL template string.

        Returns:
            A list of CTE names that the URL template depends on.

        Example:
            >>> url = 'https://example.com/{cte.field}'
            >>> dependencies = _extract_cte_dependencies(url)
            >>> dependencies
            ['cte']
        """
        dependencies: list[str] = []
        for m in _CTE_REF_FROM_URL_RE.finditer(url):
            dependencies.append(m.group(1))
        return dependencies

    def _extract_cte_name_and_fields_from_string(self, string) -> list[tuple[str, str]]:
        """Extract the CTE name and field from a URL template."""
        return _CTE_REF_FROM_URL_RE.findall(string)

    # -- grammar rules ----------------------------------------------

    def parse(self) -> WSQLProgram:
        """Top-level entry point - parse the entire WSQL program.

        Returns:
            A ``WSQLProgram`` containing zero or more CTEs and one main query.

        Raises:
            SyntaxError: On any grammar violation.
        """
        ctes: list[CTE] = []
        query_name_to_query: dict[str, WSQLQuery] = {}
        query_id_to_query: dict[int, WSQLQuery] = {}

        if self._peek_keyword('WITH'):
            self._consume_keyword('WITH')
            # Parse first CTE
            name = self._consume_identifier()
            self._consume_keyword('AS')
            self._consume_char('(')
            query = self._parse_query()
            self._consume_char(')')
            # NOTE: assign IDs
            query.id = self.query_id_counter
            self.query_id_to_name[self.query_id_counter] = name
            self.query_name_to_id[name] = self.query_id_counter
            self.query_id_counter += 1
            query.alias = name
            # TODO: remove CTE abstraction and just use WSQLQuery?
            ctes.append(CTE(name=name, query=query))
            query_name_to_query[name] = query
            query_id_to_query[query.id] = query
            # Parse additional comma-separated CTEs
            while self._try_char(','):
                name = self._consume_identifier()
                self._consume_keyword('AS')
                self._consume_char('(')
                query = self._parse_query()
                self._consume_char(')')
                # NOTE: assign IDs
                query.id = self.query_id_counter
                self.query_id_to_name[self.query_id_counter] = name
                self.query_name_to_id[name] = self.query_id_counter
                self.query_id_counter += 1
                query.alias = name
                # TODO: remove CTE abstraction and just use WSQLQuery?
                ctes.append(CTE(name=name, query=query))
                query_name_to_query[name] = query
                query_id_to_query[query.id] = query

        main_query = self._parse_query()
        main_query.alias = 'main'
        # NOTE: assign IDs
        main_query.id = self.query_id_counter
        self.query_id_to_name[self.query_id_counter] = main_query.alias
        self.query_name_to_id[main_query.alias] = self.query_id_counter
        self.query_id_counter += 1
        query_name_to_query['main'] = main_query
        query_id_to_query[main_query.id] = main_query

        if not self._at_end():
            snippet = self.src[self.pos : self.pos + 40]
            raise SyntaxError(f"Unexpected trailing content at position {self.pos}: '{snippet}...'")

        return WSQLProgram(
            ctes=ctes,
            main_query=main_query,
            query_name_to_id=self.query_name_to_id,
            query_id_to_name=self.query_id_to_name,
            query_name_to_query=query_name_to_query,
            query_id_to_query=query_id_to_query,
        )

    def _parse_query(self) -> WSQLQuery:
        """Parse ``SELECT ... FROM ... [FOLLOW ...] [DEPTH ...] [JOIN ...]``."""
        self._consume_keyword('SELECT')
        columns = self._parse_select_columns()
        self._consume_keyword('FROM')
        from_clause = self._parse_from_clause()
        joins = self._parse_join_clauses()
        return WSQLQuery(select=columns, from_clause=from_clause, joins=joins)

    # -- SELECT columns ---------------------------------------------

    def _parse_select_columns(self) -> list[SelectColumn]:
        """Parse a comma-separated list of ``SELECT`` columns.

        Each column is ``<expression> [AS <alias>]``.  CTE references
        of the form ``cte_name.field`` are automatically detected and
        annotated.
        """
        columns: list[SelectColumn] = []

        # For nameless columns, keep track of index, which will be used to generate aliases
        index = 0
        nameless_col_prefix_template = 'value_{}'
        while not self._peek_keyword('FROM'):
            if self._at_end():
                raise SyntaxError('Unexpected end of input while parsing SELECT columns')

            expr = self._capture_balanced(
                stop_keywords=self._COL_STOP_KW,
                stop_chars=',',
            )

            if not expr:
                # Skip stray commas
                if self._try_char(','):
                    continue
                break

            # Optional alias
            alias: str | None = None
            if self._peek_keyword('AS'):
                self._consume_keyword('AS')
                alias = self._consume_identifier()

            # Detect CTE reference (``cte_name.field``)
            m = _CTE_REF_RE.match(expr)
            if m:
                cte_name, cte_field = m.group(1), m.group(2)
                columns.append(
                    SelectColumn(
                        expr=expr,
                        alias=alias or cte_field,
                        is_cte_ref=True,
                        cte_name=cte_name,
                        cte_field=cte_field,
                    )
                )
            else:
                columns.append(
                    SelectColumn(
                        expr=expr,
                        alias=alias or nameless_col_prefix_template.format(index),
                    )
                )
                index += 1

            # Consume separating comma (if any)
            if not self._try_char(','):
                break

        return columns

    # -- FROM clause ------------------------------------------------

    def _parse_from_clause(self) -> FromClause:
        """Parse ``<url_or_subquery> [PER <xpath>] [FOLLOW <xpath>] [DEPTH <int>]
        [CLICK <xpath>] [PAGES <int>] [CRAWLER <ident>]``.

        Supports two source patterns:

            1. URL literal: ``FROM https://example.com ...``
            2. Subquery:    ``FROM (SELECT ... FROM ... ) ...``

        ``PER``, ``FOLLOW``, ``DEPTH``, ``CLICK``, ``PAGES``, and
        ``CRAWLER`` may appear in any order after the source. ``CLICK``
        paginates by clicking a DOM element (e.g. a "Next" button) and
        re-extracting in place, with no new network request; it requires
        ``CRAWLER playwright``. ``PAGES`` bounds the number of page
        snapshots extracted.
        """
        self._skip_ws()

        # Check for subquery: FROM (SELECT ...)
        url: str | None = None
        subquery: WSQLQuery | None = None
        cte_dependencies: list[str] = []
        projections: list[ParentQueryFieldRef] = []

        if self.pos < self.end and self.src[self.pos] == '(':
            # Peek ahead to see if this is (SELECT ...)
            saved_pos = self.pos
            self.pos += 1  # skip '('
            self._skip_ws()
            if self._peek_keyword('SELECT'):
                # It's a subquery - parse the inner query
                subquery = self._parse_query()
                self._consume_char(')')
                # Consume the alias for the subquery (i.e. "(SELECT ...) AS alias")
                self._consume_keyword('AS')
                subquery.alias = self._consume_identifier()
                assert subquery.alias is not None, 'Subquery must have an alias'
                cte_dependencies = subquery.from_clause.cte_dependencies
                projections = subquery.from_clause.expected_projections
            else:
                # Not a subquery - restore position and parse as URL
                self.pos = saved_pos
                url = self._capture_url()
                cte_dependencies = self._extract_cte_dependencies(url)
                projections = self._extract_cte_name_and_fields_from_string(url)
        else:
            url = self._capture_url()
            cte_dependencies = self._extract_cte_dependencies(url)
            projections = self._extract_cte_name_and_fields_from_string(url)

        per: str | None = None
        follow: str | None = None
        depth: int = 1
        click: str | None = None
        click_pages: int | None = None
        crawler_name: str | None = None
        found_per = found_follow = found_depth = False
        found_click = found_pages = found_crawler_name = False

        for _ in range(6):
            if not found_per and self._peek_keyword('PER'):
                self._consume_keyword('PER')
                per = self._capture_balanced(stop_keywords=self._PER_STOP_KW)
                found_per = True
            elif not found_follow and self._peek_keyword('FOLLOW'):
                self._consume_keyword('FOLLOW')
                follow = self._capture_balanced(stop_keywords=self._FOLLOW_STOP_KW)
                found_follow = True
            elif not found_depth and self._peek_keyword('DEPTH'):
                self._consume_keyword('DEPTH')
                depth = self._consume_integer()
                found_depth = True
            elif not found_click and self._peek_keyword('CLICK'):
                self._consume_keyword('CLICK')
                click = self._capture_balanced(stop_keywords=self._CLICK_STOP_KW)
                found_click = True
            elif not found_pages and self._peek_keyword('PAGES'):
                self._consume_keyword('PAGES')
                click_pages = self._consume_integer()
                found_pages = True
            elif not found_crawler_name and self._peek_keyword('CRAWLER'):
                self._consume_keyword('CRAWLER')
                crawler_name = self._consume_identifier()
                found_crawler_name = True
            else:
                break

        if follow and click:
            raise ValueError(
                'FOLLOW and CLICK cannot be combined on the same FROM clause; '
                'FOLLOW is HTTP link pagination, CLICK is in-page button pagination'
            )
        if found_click and not click:
            raise ValueError('CLICK requires a non-empty XPath')
        if found_pages and not click:
            raise ValueError('PAGES requires CLICK on the same FROM clause')
        if click_pages is not None and click_pages < 1:
            raise ValueError('PAGES must be at least 1')

        expected_projections = [
            ParentQueryFieldRef(cte_name=cte_name, cte_field=cte_field)
            for cte_name, cte_field in projections
        ]
        return FromClause(
            url=url,
            subquery=subquery,
            per=per,
            follow=follow,
            depth=depth,
            click=click,
            click_pages=click_pages,
            crawler_name=crawler_name,
            cte_dependencies=cte_dependencies,
            expected_projections=expected_projections,
        )

    # -- JOIN clauses -----------------------------------------------

    def _parse_join_clauses(self) -> list[JoinClause]:
        """Parse zero or more ``<type> JOIN <cte> [ON <expr>]`` clauses."""
        joins: list[JoinClause] = []

        while True:
            join_type: str | None = None
            for jt in ('INNER', 'LEFT', 'RIGHT', 'CROSS', 'FULL'):
                if self._peek_keyword(jt):
                    self._consume_keyword(jt)
                    join_type = jt
                    break

            if join_type is None:
                break

            self._consume_keyword('JOIN')
            cte_name = self._consume_identifier()

            on_field = ''
            if self._peek_keyword('ON'):
                self._consume_keyword('ON')
                on_field = self._capture_balanced(stop_keywords=self._ON_STOP_KW)

            joins.append(JoinClause(join_type=join_type, cte_name=cte_name, on_field=on_field))

        return joins


# --------------------------------------------------------------------
#  Transpilation - WSQL AST  ->  wxpath Segments
# --------------------------------------------------------------------


def _web_columns(columns: list[SelectColumn]) -> list[tuple[str, str]]:
    """Extract ``(alias, xpath_expr)`` pairs for web-evaluated columns."""
    return [(col.alias, col.expr) for col in columns if not col.is_cte_ref]


def _web_columns_to_map_xpath(columns: list[SelectColumn]) -> XPathWithProjection:
    """Convert ``SELECT <columns>`` to ``/map { '<alias>': <xpath_expr> }``.

    CTE-referenced columns are tracked as ``expected_projections``
    rather than included in the XPath map expression.  This prevents
    invalid XPath such as ``'alias': cte_name.field``.
    """
    expected_projections: list[ParentQueryFieldRef] = []
    web_entries: list[tuple[str, str]] = []

    for col in columns:
        if col.cte_name:
            expected_projections.append(ParentQueryFieldRef(col.cte_name, col.cte_field))
        else:
            web_entries.append((col.alias, col.expr))

    if not web_entries:
        template_string = '/map {}'
    elif len(web_entries) == 1:
        alias, expr = web_entries[0]
        template_string = f"/map {{\n    '{alias}': {expr}\n}}"
    else:
        template_string = '/map {\n'
        for alias, expr in web_entries[:-1]:
            template_string += f"    '{alias}': {expr},\n"
        last_alias, last_expr = web_entries[-1]
        template_string += f"    '{last_alias}': {last_expr}\n"
        template_string += '}'

    return XPathWithProjection(
        value=template_string,
        projection_metadata=ProjectionMetadata(expected_projections=expected_projections),
    )


def _cte_columns(columns: list[SelectColumn]) -> list[tuple[str, str]]:
    """Extract ``(cte_field, alias)`` pairs for CTE-sourced columns."""
    return [(col.cte_field, col.alias) for col in columns if col.is_cte_ref]


def _columns_to_map_xpath(columns: list[tuple[str, str]]) -> Xpath:
    """Build ``/map { 'alias': expr, ... }`` Xpath from ``(alias, expr)`` pairs."""
    if not columns:
        return Xpath('/map {}')
    lines = [f"    '{alias}': {expr}" for alias, expr in columns]
    return Xpath('/map {\n' + ',\n'.join(lines) + '\n}')


def _transpile_query(query: WSQLQuery, is_subquery: bool = False) -> Segments:
    """Transpile a single ``WSQLQuery`` (no JOINs) to wxpath Segments."""
    # ``SELECT <columns>`` to ``/map { <alias>: <xpath_expr> }``
    segments = Segments([])
    fc = query.from_clause
    if fc.subquery:
        segments.extend(_transpile_query(fc.subquery, is_subquery=True))
    else:
        if fc.expected_projections:
            # NOTE: renaming so that it doesn't conflict with the wxpath.core.parser.UrlCrawl
            #       when dispatching via get_operator
            # TODO: remove this once we have a better way to dispatch
            url_crawl_node = UrlCrawlWithProjection(
                'url_projection',
                [String(fc.url)],
                projection_metadata=ProjectionMetadata(
                    expected_projections=fc.expected_projections,
                    parent_query_name=query.alias,
                    parent_query_id=query.id,
                ),
            )
        else:
            url_crawl_node = UrlLiteral('url', [String(fc.url)])

        if fc.follow:
            url_crawl_node.__class__ = UrlCrawl
            url_crawl_node.args.append(Xpath(fc.follow))
        if fc.depth:
            # NOTE: Should this be UrlCrawl or UrlLiteral?
            # url_crawl_node.__class__ = UrlCrawl
            url_crawl_node.args.append(Depth(fc.depth))
        segments.append(url_crawl_node)

    # INVARIANT: If this is a subquery, we the column count == 1
    #            and the column select expression is a xpath expression
    #            that produces a list of URLs
    if is_subquery:
        if fc.click:
            raise ValueError('CLICK pagination is not supported inside a FROM subquery')
        assert len(query.select) == 1, 'Subquery must have exactly one SELECT column'

        url_from_xpath = UrlQuery(
            func='url',
            args=[Xpath(query.select[0].expr)],
        )
        segments.append(url_from_xpath)

    else:
        # TODO: if select column count is == 1, we should not create a map
        map_xpath = _web_columns_to_map_xpath(query.select)
        map_xpath.projection_metadata.parent_query_name = query.alias
        map_xpath.projection_metadata.parent_query_id = query.id

        if query.from_clause.per:
            map_xpath.value = query.from_clause.per + map_xpath.value

        if query.from_clause.click:
            # Emit wx:paginate(extract, click=..., pages=...) matching the
            # user's declarative click-pagination form.
            paginate_args: list = [Xpath(map_xpath.value), Click(query.from_clause.click)]
            if query.from_clause.click_pages is not None:
                paginate_args.append(Pages(query.from_clause.click_pages))
            segments.append(Paginate('//wx:paginate', paginate_args))
        else:
            segments.append(map_xpath)

    return segments


def parse(source: str) -> WSQLProgram:
    """Parse a WSQL source string into its intermediate AST.

    Args:
        source: The raw WSQL query text.

    Returns:
        A ``WSQLProgram`` containing parsed CTEs and the main query.

    Raises:
        SyntaxError: On any grammar violation in the source.
    """
    parser = WSQLParser(source)
    return parser.parse()


def transpile(program: WSQLProgram) -> Segments:
    """Transpile a WSQL program **without** JOINs to wxpath Segments.

    For programs that contain only a main query (no CTEs or JOINs),
    this function produces a ``Segments`` list that the wxpath engine
    can execute directly.

    Args:
        program: A parsed ``WSQLProgram`` whose main query has no
            ``JOIN`` clauses.

    Returns:
        wxpath ``Segments`` ready for engine execution.
    """
    return _transpile_query(program.main_query)


def transpile_cte(cte: CTE) -> Segments:
    """Transpile a single CTE's inner query to wxpath Segments.

    Args:
        cte: A ``CTE`` node from the parsed program.

    Returns:
        wxpath ``Segments`` for the CTE's sub-query.
    """
    return _transpile_query(cte.query)


def build_hgraph(program: WSQLProgram) -> dict[str, list[str]]:
    """Build a hypergraph of CTE dependencies.

    Args:
        program: A parsed ``WSQLProgram``.

    Returns:
        A dictionary of CTE names to lists of dependent CTE names.
    """
    _, dependents = _build_dependencies(program)
    hgraph: dict[str, list[str]] = {}
    all_nodes = [program.main_query.alias, *[cte.query.alias for cte in program.ctes]]
    for node in all_nodes:
        hgraph[node] = [list(dependents.get(node, []))]
    return hgraph


def _build_dependencies(
    program: WSQLProgram,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build a CTE dependency map from URL templates and JOIN clauses.

    Args:
        program: A parsed ``WSQLProgram``.

    Returns:
        A ``(dependencies, dependents)`` tuple where each maps query
        aliases to the list of query aliases they depend on (or are
        depended upon by).
    """
    dependencies: dict[str, list[str]] = defaultdict(list)
    dependents: dict[str, list[str]] = defaultdict(list)

    def _add_dep(consumer: str, provider: str) -> None:
        if provider not in dependencies[consumer]:
            dependencies[consumer].append(provider)
        if consumer not in dependents[provider]:
            dependents[provider].append(consumer)

    # FROM clause URL-template dependencies
    for dep in program.main_query.from_clause.cte_dependencies:
        _add_dep(program.main_query.alias, dep)

    # JOIN clause dependencies
    for join in program.main_query.joins:
        _add_dep(program.main_query.alias, join.cte_name)

    # CTE FROM clause dependencies
    for cte in program.ctes:
        for dep in cte.query.from_clause.cte_dependencies:
            _add_dep(cte.query.alias, dep)

    return dependencies, dependents


def _iter_root_nodes(
    program: WSQLProgram,
    dependencies: dict[str, list[str]] | None = None,
) -> Iterator[WSQLQuery]:
    """Yield queries with no dependencies (URL-template or JOIN).

    Args:
        program: A parsed ``WSQLProgram``.
        dependencies: Pre-built dependency map.  Built from *program*
            when ``None``.
    """
    if dependencies is None:
        dependencies, _ = _build_dependencies(program)

    if not dependencies.get(program.main_query.alias):
        yield program.main_query

    for cte in program.ctes:
        if not dependencies.get(cte.query.alias):
            yield cte.query
