"""Tests for the WSQL parser and transpiler.

Covers ``parse()``, ``transpile()``, dependency graph helpers, and related
models (``JoinedCrawl``, ``JoinedExtract``).
"""

from __future__ import annotations

import pytest
from wxpath.core.parser import Segments, UrlCrawl, UrlLiteral, UrlQuery, Xpath

from wsql.core.models import (
    JoinedCrawl,
    JoinedExtract,
    ProjectionMetadata,
    SelectColumn,
    XPathWithProjection,
)
from wsql.core.parser import (
    _iter_root_nodes,
    _web_columns_to_map_xpath,
    build_hgraph,
    parse,
    transpile,
)

# --------------------------------------------------------------------
#  parse()
# --------------------------------------------------------------------


def test_parse_simple_query() -> None:
    """Parse a simple SELECT FROM DEPTH query."""
    program = parse('SELECT //h1/text() AS title FROM https://example.com DEPTH 1')
    assert len(program.ctes) == 0
    assert program.main_query.from_clause.url == 'https://example.com'
    assert program.main_query.from_clause.depth == 1
    assert len(program.main_query.select) == 1
    assert program.main_query.select[0].alias == 'title'


def test_parse_with_cte_and_join() -> None:
    """Parse WITH ... AS (...) SELECT ... JOIN ..."""
    source = """
    WITH authors AS (
        SELECT //h3/text() AS name
        FROM https://quotes.toscrape.com DEPTH 1
    )
    SELECT //p/text() AS wiki_text, authors.name
    FROM https://en.wikipedia.org/wiki/{authors.name}
    DEPTH 1
    INNER JOIN authors ON authors.name
    """
    program = parse(source)
    assert len(program.ctes) == 1
    assert program.ctes[0].name == 'authors'
    assert program.main_query.joins
    assert program.main_query.joins[0].cte_name == 'authors'


def test_parse_multiple_comma_separated_ctes() -> None:
    """Parse multiple CTEs separated by commas."""
    source = """
    WITH author_bios AS (
        SELECT string(base-uri(.)) AS url, //h3/text() AS name
        FROM https://quotes.toscrape.com DEPTH 1
    ), grokipedia_pages AS (
        SELECT string(base-uri(.)) AS url, //h1/text() AS title
        FROM https://grokipedia.com DEPTH 1
    )
    SELECT author_bios.name, grokipedia_pages.title
    FROM https://example.com DEPTH 1
    INNER JOIN author_bios ON author_bios.name
    INNER JOIN grokipedia_pages ON grokipedia_pages.title
    """
    program = parse(source)
    assert len(program.ctes) == 2
    assert program.ctes[0].name == 'author_bios'
    assert program.ctes[1].name == 'grokipedia_pages'
    assert len(program.main_query.joins) == 2
    assert program.main_query.joins[0].cte_name == 'author_bios'
    assert program.main_query.joins[1].cte_name == 'grokipedia_pages'


def test_parse_from_clause_with_per_follow_depth_crawler() -> None:
    """Parse FROM clause with PER, FOLLOW, DEPTH, and CRAWLER in mixed order."""
    source = """
    SELECT //h1/text() AS title
    FROM https://example.com
    FOLLOW //a/@href
    PER //article
    CRAWLER custom_spider
    DEPTH 3
    """
    program = parse(source)
    fc = program.main_query.from_clause
    assert fc.url == 'https://example.com'
    assert fc.per == '//article'
    assert fc.follow == '//a/@href'
    assert fc.depth == 3
    assert fc.crawler_name == 'custom_spider'


def test_parse_url_template_cte_dependencies_and_projections() -> None:
    """URL templates with {cte.field} record dependencies and projections."""
    source = """
    WITH authors AS (
        SELECT //h3/text() AS name
        FROM https://quotes.toscrape.com DEPTH 1
    )
    SELECT //p/text() AS bio
    FROM https://example.com/{authors.name}/bio
    DEPTH 1
    """
    program = parse(source)
    fc = program.main_query.from_clause
    assert fc.cte_dependencies == ['authors']
    assert len(fc.expected_projections) == 1
    projection = fc.expected_projections[0]
    assert projection.cte_name == 'authors'
    assert projection.cte_field == 'name'


def test_parse_subquery_from_clause() -> None:
    """FROM (SELECT ...) AS subquery is parsed into a nested WSQLQuery."""
    source = """
    SELECT //h1/text() AS title
    FROM (
        SELECT //a/@href
        FROM https://example.com DEPTH 1
    ) AS links
    DEPTH 2
    """
    program = parse(source)
    fc = program.main_query.from_clause
    assert fc.subquery is not None
    assert fc.url is None
    assert fc.subquery.alias == 'links'
    assert fc.depth == 2
    # Subquery must have exactly one SELECT column for UrlQuery transpilation.
    assert len(fc.subquery.select) == 1


def test_parse_raises_on_trailing_content() -> None:
    """Unexpected trailing tokens after a valid query raise SyntaxError."""
    with pytest.raises(SyntaxError):
        parse('SELECT //h1 FROM https://example.com extra_tokens')


def test_parse_raises_on_missing_from() -> None:
    """Missing FROM keyword raises SyntaxError."""
    with pytest.raises(SyntaxError):
        parse('SELECT //h1')


# --------------------------------------------------------------------
#  JoinedCrawl and JoinedExtract models
# --------------------------------------------------------------------


def test_joined_crawl_creation() -> None:
    """JoinedCrawl stores root_segments and fan_out_segments."""
    root = [UrlCrawl('url', []), Xpath('/map {}')]
    fan_out = [UrlCrawl('url', []), Xpath('/map {}')]
    j = JoinedCrawl(root_segments=root, fan_out_segments=fan_out)
    assert len(j.root_segments) == 2
    assert len(j.fan_out_segments) == 2
    assert j.root_segments[0] is root[0]
    assert j.fan_out_segments[1] is fan_out[1]


def test_joined_extract_creation() -> None:
    """JoinedExtract stores (alias, expr) column pairs."""
    cols = [('a', '//h1/text()'), ('b', 'cte.x')]
    e = JoinedExtract(columns=cols)
    assert e.columns == cols
    assert len(e.columns) == 2


# --------------------------------------------------------------------
#  Transpilation helpers and public API
# --------------------------------------------------------------------


def test_web_columns_to_map_xpath_builds_projection_metadata() -> None:
    """_web_columns_to_map_xpath builds a map expression and projection metadata."""
    columns = [
        # CTE-backed column
        SelectColumn(
            expr='authors.name',
            alias='author_name',
            is_cte_ref=True,
            cte_name='authors',
            cte_field='name',
        ),
        # Web-extracted column
        SelectColumn(
            expr='//h1/text()',
            alias='title',
        ),
    ]
    xpath_with_proj = _web_columns_to_map_xpath(columns)
    assert isinstance(xpath_with_proj, XPathWithProjection)
    assert isinstance(xpath_with_proj.projection_metadata, ProjectionMetadata)
    # One CTE dependency and one web column in the map.
    assert xpath_with_proj.projection_metadata.expected_projections[0].cte_name == 'authors'
    assert 'title' in xpath_with_proj.value


def test_transpile_simple_program() -> None:
    """transpile() converts a simple program into UrlLiteral + XPathWithProjection segments."""
    program = parse('SELECT //h1/text() AS title FROM https://example.com DEPTH 2')
    segments = transpile(program)
    assert isinstance(segments, Segments)
    assert len(segments) == 2
    url_seg, map_seg = segments[0], segments[1]
    assert isinstance(url_seg, UrlLiteral)
    assert url_seg.func == 'url'
    assert url_seg.args[0].value == 'https://example.com'
    assert isinstance(map_seg, Xpath)
    assert '/map' in map_seg.value


def test_transpile_respects_per_prefix() -> None:
    """PER clause is prefixed onto the generated /map expression."""
    program = parse('SELECT //h1/text() AS title FROM https://example.com PER //article DEPTH 1')
    segments = transpile(program)
    map_seg = segments[-1]
    assert isinstance(map_seg, Xpath)
    assert map_seg.value.startswith('//article')


def test_transpile_subquery_uses_url_query() -> None:
    """Subquery FROM source uses UrlQuery followed by map XPath."""
    source = """
    SELECT //h1/text() AS title
    FROM (
        SELECT //a/@href
        FROM https://example.com DEPTH 1
    ) AS links
    DEPTH 1
    """
    program = parse(source)
    segments = transpile(program)
    # Expect two segments from subquery (Url literal + UrlQuery) and one map for main query.
    assert any(isinstance(seg, UrlQuery) for seg in segments)
    assert any(isinstance(seg, Xpath) for seg in segments)


def test_transpile_cte_uses_projection_url_crawl() -> None:
    """CTE URL templates produce UrlCrawlWithProjection segments when transpiled."""
    source = """
    WITH authors AS (
        SELECT //h3/text() AS name
        FROM https://quotes.toscrape.com DEPTH 1
    )
    SELECT //p/text() AS bio, authors.name
    FROM https://example.com/{authors.name}/bio
    DEPTH 1
    INNER JOIN authors ON authors.name
    """
    program = parse(source)
    # Transpile the main query.
    segments = transpile(program)
    url_seg = segments[0]
    # The first segment should be a crawl-like node with projection metadata.
    assert hasattr(url_seg, 'projection_metadata')
    assert url_seg.projection_metadata.expected_projections


def test_build_hgraph_and_iter_root_nodes() -> None:
    """build_hgraph() and _iter_root_nodes() respect CTE dependencies."""
    source = """
    WITH authors AS (
        SELECT //h3/text() AS name
        FROM https://quotes.toscrape.com DEPTH 1
    )
    SELECT //p/text() AS bio, authors.name
    FROM https://example.com/{authors.name}/bio
    DEPTH 1
    INNER JOIN authors ON authors.name
    """
    program = parse(source)
    hgraph = build_hgraph(program)
    # Both main and authors should appear as nodes.
    assert 'main' in hgraph
    assert 'authors' in hgraph
    # Authors should have main as a dependent.
    assert 'main' in hgraph['authors'][0]

    roots = list(_iter_root_nodes(program))
    # Only the authors CTE is dependency-free.
    assert len(roots) == 1
    assert roots[0].alias == 'authors'
