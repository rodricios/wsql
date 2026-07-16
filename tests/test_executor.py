"""Tests for the WSQL executor (runtime orchestration).

Uses mock engines and segments to verify CTE orchestration without
requiring live HTTP requests.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from wxpath.core.parser import Segments
from wxpath.core.runtime.engine import WXPathEngine
from wxpath.http.client.crawler import Crawler

from wsql.core.parser import parse
from wsql.runtime.executor import WSQLExecutor, wsql_execute

# --------------------------------------------------------------------
#  Mock engine
# --------------------------------------------------------------------


class MockEngine:
    """Simulates wxpath engine for testing CTE orchestration.

    Returns pre-programmed responses based on the segments passed to
    ``run()``. This allows testing the executor's CTE collection and
    main query flow without actual HTTP requests.

    Args:
        responses: Mapping of segment representation -> list of results
            to yield.
    """

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        self.responses = responses or {}
        self.run_calls: list[tuple[Any, int]] = []

    async def run(
        self,
        expression: str | Segments,
        max_depth: int,
        progress: bool = False,
        yield_errors: bool = False,
    ) -> AsyncGenerator[Any, None]:
        """Simulate wxpath engine's run method."""
        # Record the call for assertions
        self.run_calls.append((expression, max_depth))

        # Return pre-programmed results
        key = str(expression)
        results = self.responses.get(key, [])

        for result in results:
            yield result


# --------------------------------------------------------------------
#  Engine / crawler wiring
# --------------------------------------------------------------------


class _TestExecutorWithMockEngine(WSQLExecutor):
    """Test-only executor that injects a mock engine implementation."""

    def __init__(self, mock_engine: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._mock_engine = mock_engine

    def _create_engine(
        self,
        crawler_name: str | None = None,
        from_clause: Any = None,
    ) -> Any:
        return self._mock_engine


def test_executor_rejects_legacy_engine_argument() -> None:
    """Legacy engine kwarg is rejected with migration guidance."""
    with pytest.raises(TypeError, match='engine'):
        WSQLExecutor(engine=MagicMock(spec=WXPathEngine))


def test_executor_rejects_legacy_crawler_argument() -> None:
    """Legacy crawler kwarg is rejected with migration guidance."""
    with pytest.raises(TypeError, match='default_crawler_name'):
        WSQLExecutor(crawler=MagicMock(spec=Crawler))


def test_executor_inline_crawler_overrides_default_crawler_name() -> None:
    """Inline query crawler name takes precedence over default crawler name."""
    fake_crawler = MagicMock(name='inline-crawler')
    fake_crawler_cls = MagicMock(return_value=fake_crawler)

    executor = WSQLExecutor(
        default_crawler_name='playwright',
        concurrency=32,
        per_host=4,
        respect_robots=False,
        verify_ssl=False,
        headers={'X-Test': '1'},
    )

    with patch(
        'wsql.runtime.executor._get_crawler_class',
        return_value=fake_crawler_cls,
    ) as mocked_get_class:
        crawler = executor._create_engine('aiohttp').crawler

    assert crawler is fake_crawler
    mocked_get_class.assert_called_once_with('aiohttp')
    fake_crawler_cls.assert_called_once_with(
        concurrency=32,
        per_host=4,
        respect_robots=False,
        verify_ssl=False,
        headers={'X-Test': '1'},
    )


def test_executor_inline_crawler_overrides_default_name() -> None:
    """Inline crawler takes precedence over default crawler name."""
    inline_crawler = MagicMock(name='inline-crawler')
    fake_crawler_cls = MagicMock(return_value=inline_crawler)

    executor = WSQLExecutor(
        default_crawler_name='playwright',
        concurrency=10,
        per_host=2,
        respect_robots=False,
    )

    with patch(
        'wsql.runtime.executor._get_crawler_class',
        return_value=fake_crawler_cls,
    ) as mocked_get_class:
        crawler = executor._create_engine('aiohttp').crawler

    assert crawler is inline_crawler
    mocked_get_class.assert_called_once_with('aiohttp')
    fake_crawler_cls.assert_called_once_with(
        concurrency=10,
        per_host=2,
        respect_robots=False,
        verify_ssl=True,
        headers=None,
    )


@pytest.mark.asyncio
async def test_execute_uses_inline_crawler_name_over_defaults() -> None:
    """Execution path passes inline crawler name into engine creation."""
    query = 'SELECT //h1/text() AS title FROM https://example.com CRAWLER aiohttp DEPTH 1'

    async def mock_run(*args, **kwargs):
        yield {'title': 'Inline crawler wins'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(
        mock_engine,
        default_crawler_name='playwright',
    )
    with patch.object(executor, '_create_engine', return_value=mock_engine) as mocked_create:
        results = await executor.execute_collect(query)

    assert len(results) == 1
    assert results[0]['title'] == 'Inline crawler wins'
    assert mocked_create.call_count == 1
    assert mocked_create.call_args.args[0] == 'aiohttp'
    assert mocked_create.call_args.args[1].crawler_name == 'aiohttp'


def test_executor_uses_default_crawler_name_when_query_omits_crawler() -> None:
    """Default crawler name is used when query does not set CRAWLER inline."""
    fake_crawler = MagicMock(name='default-crawler')
    fake_crawler_cls = MagicMock(return_value=fake_crawler)

    executor = WSQLExecutor(
        default_crawler_name='playwright',
        playwright_stealth=True,
    )

    with patch(
        'wsql.runtime.executor._get_crawler_class',
        return_value=fake_crawler_cls,
    ) as mocked_get_class:
        crawler = executor._create_engine().crawler

    assert crawler is fake_crawler
    mocked_get_class.assert_called_once_with('playwright')
    assert fake_crawler_cls.call_args.kwargs['stealth'] is True


def test_executor_allows_engine_to_create_default_crawler_when_unspecified() -> None:
    """Executor defers crawler creation to engine when no crawler defaults exist."""
    executor = WSQLExecutor()
    engine = executor._create_engine()

    assert isinstance(engine, WXPathEngine)
    assert isinstance(engine.crawler, Crawler)


def test_executor_click_pagination_requires_playwright_crawler() -> None:
    """CLICK pagination without CRAWLER playwright raises a clear error."""
    executor = WSQLExecutor()
    query = parse(
        "SELECT //td/text() AS name FROM https://example.com "
        "PER //tr CLICK //a[@class='next']"
    )
    with pytest.raises(ValueError, match='CLICK pagination requires CRAWLER playwright'):
        executor._create_engine(
            query.main_query.from_clause.crawler_name,
            query.main_query.from_clause,
        )


def test_executor_click_pagination_accepts_inline_crawler_playwright() -> None:
    """CLICK pagination with inline CRAWLER playwright is accepted."""
    executor = WSQLExecutor()
    query = parse(
        "SELECT //td/text() AS name FROM https://example.com "
        "PER //tr CLICK //a[@class='next'] CRAWLER playwright"
    )
    engine = executor._create_engine(
        query.main_query.from_clause.crawler_name,
        query.main_query.from_clause,
    )
    assert isinstance(engine, WXPathEngine)


def test_executor_click_pagination_accepts_default_crawler_playwright() -> None:
    """CLICK pagination validated against the executor's default_crawler_name."""
    executor = WSQLExecutor(default_crawler_name='playwright')
    query = parse(
        "SELECT //td/text() AS name FROM https://example.com "
        "PER //tr CLICK //a[@class='next']"
    )
    engine = executor._create_engine(
        query.main_query.from_clause.crawler_name,
        query.main_query.from_clause,
    )
    assert isinstance(engine, WXPathEngine)


# --------------------------------------------------------------------
#  Simple query (no CTEs)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_query_no_ctes() -> None:
    """Simple SELECT FROM query without CTEs or JOINs."""
    query = 'SELECT //h1/text() AS title FROM https://example.com DEPTH 1'
    program = parse(query)

    mock_engine = MockEngine(
        responses={
            str(Segments): [{'title': 'Hello World'}],  # fallback match
        }
    )

    # Override responses to match any segments
    async def mock_run(*args, **kwargs):
        yield {'title': 'Hello World'}

    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    assert len(results) == 1
    assert results[0] == {'title': 'Hello World'}


class _XPathMapLike:
    """Minimal stand-in for elementpath XPathMap engine output."""

    def __init__(self, mapping: dict[str, object]) -> None:
        self._map = mapping


@pytest.mark.asyncio
async def test_simple_query_normalizes_xpath_map_output() -> None:
    """Simple mode normalizes XPathMap-like engine output to plain dict rows."""
    query = 'SELECT //h1/text() AS title FROM https://example.com DEPTH 1'
    program = parse(query)

    async def mock_run(*args, **kwargs):
        yield _XPathMapLike({'title': 'Hello World'})

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    assert len(results) == 1
    assert results[0] == {'title': 'Hello World'}
    assert isinstance(results[0], dict)


@pytest.mark.asyncio
async def test_simple_query_with_multiple_results() -> None:
    """Query that yields multiple result dictionaries."""
    query = 'SELECT //p/text() AS text FROM https://example.com DEPTH 1'
    program = parse(query)

    async def mock_run(*args, **kwargs):
        yield {'text': 'Paragraph 1'}
        yield {'text': 'Paragraph 2'}
        yield {'text': 'Paragraph 3'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    assert len(results) == 3
    assert results[0]['text'] == 'Paragraph 1'
    assert results[2]['text'] == 'Paragraph 3'


# --------------------------------------------------------------------
#  CTE collection
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cte_collection() -> None:
    """CTE results stream directly into main query (horizontal execution)."""
    query = """
        WITH authors AS (
            SELECT //h3/text() AS name
            FROM https://quotes.example.com
            DEPTH 1
        )
        SELECT authors.name
        FROM https://example.com
        DEPTH 1
        INNER JOIN authors ON authors.name
    """
    program = parse(query)

    cte_run_count = 0
    main_run_count = 0

    async def mock_run(expression, max_depth, **kwargs):
        nonlocal cte_run_count, main_run_count

        # Heuristic: CTE has different depth or different segments
        if max_depth == 1 and 'quotes.example.com' in str(expression):
            # CTE execution
            cte_run_count += 1
            yield {'name': 'Author A'}
            yield {'name': 'Author B'}
        else:
            # Main query execution (one per CTE row in horizontal mode)
            main_run_count += 1
            yield {'name': f'Result {main_run_count}'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    # In horizontal mode: 1 CTE run + 2 main runs (one per CTE row)
    assert cte_run_count == 1
    assert main_run_count == 2
    assert len(results) == 2


@pytest.mark.asyncio
async def test_multiple_ctes() -> None:
    """Multiple CTEs - only the primary JOIN CTE uses horizontal execution."""
    query = """
        WITH cte1 AS (
            SELECT //h1/text() AS title
            FROM https://a.com
            DEPTH 1
        ), cte2 AS (
            SELECT //p/text() AS body
            FROM https://b.com
            DEPTH 1
        )
        SELECT cte1.title, cte2.body
        FROM https://c.com
        DEPTH 1
        INNER JOIN cte1 ON cte1.title
    """
    program = parse(query)

    cte1_runs = 0
    main_runs = 0

    async def mock_run(expression, max_depth, **kwargs):
        nonlocal cte1_runs, main_runs

        if 'a.com' in str(expression):
            # CTE 1 (primary join target)
            cte1_runs += 1
            yield {'title': 'Title'}
        else:
            # Main query (executed once per CTE1 row)
            main_runs += 1
            yield {'title': 'Title', 'body': 'Body'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    # Horizontal mode: 1 CTE run + 1 main run (1 CTE row -> 1 main execution)
    assert cte1_runs == 1
    assert main_runs == 1
    assert len(results) == 1


@pytest.mark.asyncio
async def test_multiple_comma_separated_ctes() -> None:
    """Multiple comma-separated CTEs with multiple JOINs."""
    query = """
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
    program = parse(query)
    assert len(program.ctes) == 2
    assert len(program.main_query.joins) == 2

    run_order: list[str] = []

    async def mock_run(expression, max_depth, **kwargs):
        # Classify by segment shape: CTE runs are UrlCrawl/UrlLiteral; main is JoinSeed.
        # Main query segments contain JoinSeed and url_template example.com; CTE results
        # in JoinSeed also contain URLs, so we distinguish by presence of JoinSeed.
        s = str(expression)
        if 'JoinSeed' in s or 'join_seed' in s:
            run_order.append('main')
            yield {'name': 'Author A', 'title': 'Page Title'}
        elif 'quotes.toscrape.com' in s and 'example.com' not in s:
            run_order.append('cte_a')
            yield {'url': 'https://quotes.toscrape.com/author/1', 'name': 'Author A'}
        elif 'grokipedia.com' in s:
            run_order.append('cte_b')
            yield {'url': 'https://grokipedia.com/page/1', 'title': 'Page Title'}
        else:
            run_order.append('main')
            yield {'name': 'Author A', 'title': 'Page Title'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    # Both CTEs run once, then main query runs once per primary CTE row
    assert run_order.count('cte_a') == 1
    assert run_order.count('cte_b') == 1
    assert run_order.count('main') == 1
    assert len(results) == 1


# --------------------------------------------------------------------
#  Pipelined horizontal execution (no full CTE materialization)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_horizontal_execution_streaming_per_row() -> None:
    """Dependent runs overlap in horizontal mode (bounded parallelism)."""
    query = """
        WITH authors AS (
            SELECT //h3/text() AS name
            FROM https://quotes.example.com
            DEPTH 1
        )
        SELECT authors.name
        FROM https://example.com
        DEPTH 1
        INNER JOIN authors ON authors.name
    """
    program = parse(query)

    inflight_main = 0
    max_inflight_main = 0
    main_runs = 0

    async def mock_run(expression, max_depth, **kwargs):
        nonlocal inflight_main, max_inflight_main, main_runs
        is_cte = 'quotes.example.com' in str(expression)
        if is_cte:
            for name in ('First', 'Second', 'Third'):
                yield {'name': name}
        else:
            main_runs += 1
            inflight_main += 1
            max_inflight_main = max(max_inflight_main, inflight_main)
            await asyncio.sleep(0.01)
            yield {'name': 'Result'}
            inflight_main -= 1

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine, concurrency=2)
    results = await executor.execute_collect(program)

    # One dependent run per driver row; at least two overlap.
    assert main_runs == 3
    assert max_inflight_main >= 2
    assert len(results) == 3


# --------------------------------------------------------------------
#  Multiple JOINs (materialize all, then main per driver row)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_joins_driver_streams_non_driver_materialized() -> None:
    """With multiple JOINs, driver CTE streams and non-driver CTEs are materialized."""
    query = """
        WITH a AS (
            SELECT //h1/text() AS x FROM https://a.com DEPTH 1
        ), b AS (
            SELECT //p/text() AS y FROM https://b.com DEPTH 1
        )
        SELECT a.x
        FROM https://example.com/{a.x}
        DEPTH 1
        INNER JOIN a ON a.x
        INNER JOIN b ON b.y
    """
    program = parse(query)

    cte_a_runs = 0
    cte_b_runs = 0
    main_runs = 0

    async def mock_run(expression, max_depth, **kwargs):
        nonlocal cte_a_runs, cte_b_runs, main_runs
        s = str(expression)
        if 'a.com' in s:
            cte_a_runs += 1
            yield {'x': 'X1'}
        elif 'b.com' in s:
            cte_b_runs += 1
            yield {'y': 'Y1'}
        else:
            main_runs += 1
            yield {'x': 'X1'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    # Driver CTE (a) streams, non-driver CTE (b) is materialized, then
    # main runs once per driver row.
    assert cte_a_runs == 1
    assert cte_b_runs == 1
    assert main_runs == 1
    assert len(results) == 1


@pytest.mark.asyncio
async def test_multilevel_dag_pipeline_executes_sequential_dependencies() -> None:
    """Sequential dependency DAG executes in level order across contexts."""
    query = """
        WITH author_bios AS (
            SELECT
                string(base-uri(.)) AS url,
                //h3/text() AS name
            FROM https://quotes.toscrape.com
            DEPTH 1
        ), wikipedia_pages AS (
            SELECT
                author_bios.name,
                author_bios.url AS quotes_toscrape_url,
                string(base-uri(.)) AS wikipedia_url
            FROM https://en.wikipedia.org/wiki/{author_bios.name}
            DEPTH 1
        )
        SELECT
            string(base-uri(.)) AS google_url,
            wikipedia_pages.name AS wikipedia_name,
            wikipedia_pages.wikipedia_url AS wikipedia_url,
            author_bios.url AS quotes_toscrape_url,
            author_bios.name AS quotes_toscrape_name
        FROM https://www.google.com/search?q={wikipedia_pages.wikipedia_url}
        DEPTH 1
        INNER JOIN author_bios ON author_bios.name
        INNER JOIN wikipedia_pages ON wikipedia_pages.wikipedia_url
    """
    program = parse(query)

    wiki_inputs: list[str] = []
    google_inputs: list[str] = []

    async def mock_run(expression, max_depth, **kwargs):
        first_seg = expression[0] if expression else None
        template_url = ''
        if first_seg is not None and hasattr(first_seg, 'args') and first_seg.args:
            first_arg = first_seg.args[0]
            template_url = getattr(first_arg, 'value', '')

        if 'quotes.toscrape.com' in template_url:
            yield {'url': 'https://quotes.toscrape.com/author/a', 'name': 'AuthorA'}
            yield {'url': 'https://quotes.toscrape.com/author/b', 'name': 'AuthorB'}
        elif 'en.wikipedia.org/wiki/' in template_url:
            assert first_seg is not None
            projected_values = first_seg.projection_metadata.projected_values
            author_name = next(iter(projected_values.values()), None)
            wiki_inputs.append(author_name)
            yield {'wikipedia_url': f'https://en.wikipedia.org/wiki/{author_name}'}
        elif 'google.com/search?q=' in template_url:
            assert first_seg is not None
            projected_values = first_seg.projection_metadata.projected_values
            wiki_url = next(iter(projected_values.values()), None)
            google_inputs.append(wiki_url)
            yield {'google_url': f'https://www.google.com/search?q={wiki_url}'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    assert sorted(wiki_inputs) == ['AuthorA', 'AuthorB']
    assert sorted(google_inputs) == [
        'https://en.wikipedia.org/wiki/AuthorA',
        'https://en.wikipedia.org/wiki/AuthorB',
    ]
    assert len(results) == 2
    assert {row['wikipedia_name'] for row in results} == {'AuthorA', 'AuthorB'}
    assert all('quotes_toscrape_url' in row for row in results)


@pytest.mark.asyncio
async def test_branching_dag_pipeline_combines_parallel_dependencies() -> None:
    """Branching DAG combines contexts from sibling dependencies into main."""
    query = """
        WITH root AS (
            SELECT
                string(base-uri(.)) AS root_url,
                //h3/text() AS name
            FROM https://root.example.com
            DEPTH 1
        ), dep_a AS (
            SELECT
                root.name,
                string(base-uri(.)) AS a_url
            FROM https://a.example.com/{root.name}
            DEPTH 1
        ), dep_b AS (
            SELECT
                root.name,
                string(base-uri(.)) AS b_url
            FROM https://b.example.com/{root.name}
            DEPTH 1
        )
        SELECT
            string(base-uri(.)) AS main_url,
            dep_a.a_url AS dep_a_url,
            dep_b.b_url AS dep_b_url,
            root.name AS root_name
        FROM https://main.example.com/{dep_a.a_url}/{dep_b.b_url}
        DEPTH 1
        INNER JOIN root ON root.name
        INNER JOIN dep_a ON dep_a.a_url
        INNER JOIN dep_b ON dep_b.b_url
    """
    program = parse(query)

    dep_a_inputs: list[str] = []
    dep_b_inputs: list[str] = []
    main_projected_pairs: list[tuple[str | None, str | None]] = []

    async def mock_run(expression, max_depth, **kwargs):
        first_seg = expression[0] if expression else None
        template_url = ''
        if first_seg is not None and hasattr(first_seg, 'args') and first_seg.args:
            first_arg = first_seg.args[0]
            template_url = getattr(first_arg, 'value', '')

        if 'root.example.com' in template_url:
            yield {'name': 'AuthorA', 'root_url': 'https://root.example.com/a'}
            yield {'name': 'AuthorB', 'root_url': 'https://root.example.com/b'}
        elif 'a.example.com/' in template_url:
            assert first_seg is not None
            author_name = first_seg.projection_metadata.projected_values.get('root.name')
            dep_a_inputs.append(author_name)
            yield {'a_url': f'a-{author_name}'}
        elif 'b.example.com/' in template_url:
            assert first_seg is not None
            author_name = first_seg.projection_metadata.projected_values.get('root.name')
            dep_b_inputs.append(author_name)
            yield {'b_url': f'b-{author_name}'}
        elif 'main.example.com/' in template_url:
            assert first_seg is not None
            projected = first_seg.projection_metadata.projected_values
            a_val = projected.get('dep_a.a_url')
            b_val = projected.get('dep_b.b_url')
            main_projected_pairs.append((a_val, b_val))
            yield {'main_url': f'https://main.example.com/{a_val}/{b_val}'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine, concurrency=3)
    results = await executor.execute_collect(program)

    assert sorted(dep_a_inputs) == ['AuthorA', 'AuthorB']
    assert sorted(dep_b_inputs) == ['AuthorA', 'AuthorB']
    assert sorted(main_projected_pairs) == [
        ('a-AuthorA', 'b-AuthorA'),
        ('a-AuthorB', 'b-AuthorB'),
    ]
    assert len(results) == 2
    assert {row['root_name'] for row in results} == {'AuthorA', 'AuthorB'}
    assert all('dep_a_url' in row and 'dep_b_url' in row for row in results)


# --------------------------------------------------------------------
#  Multiple CTEs with no dependencies (simple mode)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_ctes_no_dependencies_simple_mode() -> None:
    """Multiple CTEs with no JOIN: all CTEs run in parallel, then main query."""
    query = """
        WITH cte1 AS (
            SELECT //h1/text() AS t FROM https://site1.com DEPTH 1
        ), cte2 AS (
            SELECT //p/text() AS b FROM https://site2.com DEPTH 1
        )
        SELECT //title/text() AS title
        FROM https://main.com
        DEPTH 1
    """
    program = parse(query)
    assert not program.has_joins()

    runs = []

    async def mock_run(expression, max_depth, **kwargs):
        s = str(expression)
        if 'site1.com' in s:
            runs.append('cte1')
            yield {'t': 'T1'}
        elif 'site2.com' in s:
            runs.append('cte2')
            yield {'b': 'B1'}
        else:
            runs.append('main')
            yield {'title': 'Main'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program)

    # Main query ran; CTEs ran (order may vary)
    assert 'main' in runs
    assert len(results) == 1
    assert results[0]['title'] == 'Main'


# --------------------------------------------------------------------
#  Error handling
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yield_errors_in_cte() -> None:
    """Error dictionaries from CTEs are yielded when yield_errors=True."""
    query = """
        WITH data AS (
            SELECT //h1/text() AS title
            FROM https://broken.com
            DEPTH 1
        )
        SELECT data.title
        FROM https://example.com
        DEPTH 1
        INNER JOIN data ON data.title
    """
    program = parse(query)

    async def mock_run(expression, max_depth, **kwargs):
        yield {'__type__': 'error', 'reason': 'network_error', 'url': 'https://broken.com'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program, yield_errors=True)

    # Error from CTE should appear in results
    assert any(r.get('__type__') == 'error' for r in results)


@pytest.mark.asyncio
async def test_yield_errors_disabled() -> None:
    """Error dictionaries are filtered when yield_errors=False."""
    query = 'SELECT //h1/text() AS title FROM https://example.com DEPTH 1'
    program = parse(query)

    async def mock_run(expression, max_depth, **kwargs):
        yield_errors = kwargs.get('yield_errors', False)
        if yield_errors:
            yield {'__type__': 'error', 'reason': 'test'}
        yield {'title': 'Success'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(program, yield_errors=False)

    # Only non-error results
    assert len(results) == 1
    assert results[0]['title'] == 'Success'


# --------------------------------------------------------------------
#  Synchronous API
# --------------------------------------------------------------------


def test_wsql_execute_blocking() -> None:
    """Synchronous wsql_execute() wrapper works correctly."""
    query = 'SELECT //h1/text() AS title FROM https://example.com DEPTH 1'

    # Create a mock executor
    mock_executor = MagicMock(spec=WSQLExecutor)

    async def mock_execute_collect(*args, **kwargs):
        return [{'title': 'Sync Result'}]

    mock_executor.execute_collect = mock_execute_collect

    results = wsql_execute(query, executor=mock_executor)

    assert len(results) == 1
    assert results[0]['title'] == 'Sync Result'


def test_wsql_execute_restores_previous_event_loop() -> None:
    """wsql_execute restores a previously active event loop."""
    query = 'SELECT //h1/text() AS title FROM https://example.com DEPTH 1'
    previous_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(previous_loop)

    mock_executor = MagicMock(spec=WSQLExecutor)

    async def mock_execute_collect(*args, **kwargs):
        return [{'title': 'Loop restore'}]

    mock_executor.execute_collect = mock_execute_collect

    try:
        results = wsql_execute(query, executor=mock_executor)
        assert len(results) == 1
        assert results[0]['title'] == 'Loop restore'
        assert asyncio.get_event_loop() is previous_loop
    finally:
        previous_loop.close()
        asyncio.set_event_loop(None)


def test_wsql_execute_does_not_leave_closed_current_loop() -> None:
    """wsql_execute does not leave a closed loop bound as current loop."""
    query = 'SELECT //h1/text() AS title FROM https://example.com DEPTH 1'
    asyncio.set_event_loop(None)

    mock_executor = MagicMock(spec=WSQLExecutor)

    async def mock_execute_collect(*args, **kwargs):
        return [{'title': 'No closed loop'}]

    mock_executor.execute_collect = mock_execute_collect

    results = wsql_execute(query, executor=mock_executor)
    assert len(results) == 1
    assert results[0]['title'] == 'No closed loop'

    fresh_loop = asyncio.get_event_loop()
    try:
        assert not fresh_loop.is_closed()
    finally:
        fresh_loop.close()
        asyncio.set_event_loop(None)


# --------------------------------------------------------------------
#  Max depth propagation
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_depth_from_query() -> None:
    """max_depth is correctly extracted from FROM clause."""
    query = 'SELECT //h1/text() AS title FROM https://example.com DEPTH 5'
    program = parse(query)

    received_depth = None

    async def mock_run(expression, max_depth, **kwargs):
        nonlocal received_depth
        received_depth = max_depth
        yield {'title': 'Test'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    await executor.execute_collect(program)

    assert received_depth == 5


@pytest.mark.asyncio
async def test_cte_depth_independent() -> None:
    """CTE and main query can have different DEPTH values."""
    query = """
        WITH data AS (
            SELECT //h1/text() AS title
            FROM https://a.com
            DEPTH 3
        )
        SELECT data.title
        FROM https://b.com
        DEPTH 7
        INNER JOIN data ON data.title
    """
    program = parse(query)

    depths_seen = []

    async def mock_run(expression, max_depth, **kwargs):
        depths_seen.append(max_depth)
        if len(depths_seen) == 1:
            yield {'title': 'CTE Result'}
        else:
            yield {'title': 'Main Result'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    await executor.execute_collect(program)

    assert depths_seen[0] == 3  # CTE depth
    assert depths_seen[1] == 7  # Main query depth


# --------------------------------------------------------------------
#  Integration sanity check
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_with_real_parser() -> None:
    """Full parse -> transpile -> execute flow (with mocked engine)."""
    source = """
        SELECT 
            //h1/text() AS title,
            //p/text() AS body
        FROM https://example.com
        DEPTH 1
    """

    async def mock_run(*args, **kwargs):
        yield {'title': 'Example', 'body': 'Paragraph'}

    mock_engine = MockEngine()
    mock_engine.run = mock_run

    executor = _TestExecutorWithMockEngine(mock_engine)
    results = await executor.execute_collect(source)

    assert len(results) == 1
    assert 'title' in results[0]
    assert 'body' in results[0]
