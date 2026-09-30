"""WSQL Executor - Orchestrates query execution over wxpath engine.

This module provides a thin orchestration layer that coordinates
execution across CTEs and the main query:

    **Simple Mode** (no JOINs):
        Execute CTEs in parallel, then execute the main query.
        CTE results are not used by the main query.

    **Horizontal Mode** (with JOINs):
        JOINs execute **horizontally**: each CTE row produces a main
        query fetch. CTE results stream into main query URL templates.

        For ``INNER JOIN``: each CTE row becomes a URL, all URLs are
        fetched, and results are merged with CTE data.

The key mental model:
    - **No JOIN** -> CTE and main query are independent (vertical)
    - **With JOIN** -> CTE feeds main query (horizontal, data flows right ->)

Example
~~~~~~~

    >>> from wsql import wsql_execute
    >>>
    >>> query = '''
    ... WITH authors AS (
    ...     SELECT //h3/text() AS name
    ...     FROM https://quotes.toscrape.com DEPTH 1
    ... )
    ... SELECT //p/text() AS wiki_text, authors.name
    ... FROM https://en.wikipedia.org/wiki/{authors.name}
    ... DEPTH 1
    ... INNER JOIN authors ON authors.name
    ... '''
    >>>
    >>> results = wsql_execute(query)
    >>> # Each CTE row produces a main query fetch (horizontal execution)
"""

from __future__ import annotations

import asyncio
import copy
from collections import defaultdict, deque
from typing import Any, AsyncGenerator

from wxpath.core.parser import Segments
from wxpath.core.runtime.engine import WXPathEngine
from wxpath.http.client import crawler as crawler_module

from wsql.core.models import (
    CTE,
    FromClause,
    UrlCrawlWithProjection,
    WSQLProgram,
    WSQLQuery,
)
from wsql.core.parser import (
    _build_dependencies,
    _iter_root_nodes,
    _transpile_query,
    parse,
    transpile,
    transpile_cte,
)
from wsql.util.logging import get_logger
from wsql.util.rows import row_to_dict

log = get_logger(__name__)


def _get_effective_max_depth(query: WSQLQuery) -> int:
    """Extract the effective max crawl depth from a query.

    For queries with subquery sources, the effective depth is the
    *inner* subquery's depth.
    """
    fc = query.from_clause
    if fc.subquery:
        return _get_effective_max_depth(fc.subquery)
    return fc.depth


Crawler = crawler_module.Crawler
try:
    PlaywrightCrawler = crawler_module.PlaywrightCrawler
except AttributeError:
    PlaywrightCrawler = None
try:
    FlareSolverrCrawler = crawler_module.FlareSolverrCrawler
except AttributeError:
    FlareSolverrCrawler = None

CRAWLER_NAME_TO_CRAWLER_CLASS: dict[str, type[Any]] = {
    'default': Crawler,
    'aiohttp': Crawler,
}
if PlaywrightCrawler is not None:
    CRAWLER_NAME_TO_CRAWLER_CLASS['playwright'] = PlaywrightCrawler
if FlareSolverrCrawler is not None:
    CRAWLER_NAME_TO_CRAWLER_CLASS['flaresolverr'] = FlareSolverrCrawler


def _get_crawler_class(crawler_name: str | None = None) -> type[Any]:
    """Get the crawler class for a given crawler name."""
    if crawler_name is None:
        return Crawler
    # TODO: Change this runtime error to a parse error.
    if crawler_name not in CRAWLER_NAME_TO_CRAWLER_CLASS:
        raise ValueError(f'Invalid crawler name: {crawler_name}')
    return CRAWLER_NAME_TO_CRAWLER_CLASS[crawler_name]


def _validate_click_requires_playwright(
    from_clause: FromClause,
    resolved_crawler_name: str | None,
) -> None:
    """Ensure ``CLICK`` pagination only runs under the playwright crawler.

    Raises:
        ValueError: If ``from_clause.click`` is set but the resolved
            crawler is not ``\"playwright\"``.
    """
    if from_clause.click and resolved_crawler_name != 'playwright':
        raise ValueError(
            'CLICK pagination requires CRAWLER playwright (got '
            f'{resolved_crawler_name!r}). Add CRAWLER playwright to the '
            'FROM clause or configure default_crawler_name="playwright".'
        )


class WSQLExecutor:
    """High-level executor for WSQL programs.

    Execution strategy is determined by the presence of JOINs:

        **No JOINs**: Execute CTEs in parallel, then main query.
        **With JOINs**: Execute horizontally - each CTE row produces
            a main query fetch.

    All HTTP fetching is delegated to the wxpath engine.

    Args:
        default_crawler_name: Default crawler type used when query does not
            provide ``CRAWLER <name>`` inline.
        concurrency: Number of concurrent fetches.
        per_host: Number of concurrent fetches per host.
        respect_robots: Whether to respect ``robots.txt``.
        verify_ssl: Whether TLS certificate verification is enabled.
        headers: Optional HTTP headers for crawler requests.
        playwright_stealth: Whether to enable Playwright stealth mode.
        flaresolverr_url: FlareSolverr API endpoint when that crawler is used.
        yield_errors: Whether to yield error dictionaries for failures.

    Notes:
        ``default_crawler_name`` is the public crawler selection API.
        ``WSQLExecutor`` intentionally does not accept a pre-built engine,
        because shared engine instances are not concurrency-safe across
        independent query executions.
    """

    def __init__(
        self,
        default_crawler_name: str | None = None,
        concurrency: int = 16,
        per_host: int = 8,
        respect_robots: bool = False,
        verify_ssl: bool = True,
        headers: dict[str, str] | None = None,
        playwright_stealth: bool = False,
        flaresolverr_url: str = 'http://localhost:8191/v1',
        yield_errors: bool = False,
        download_delay: float | None = None,
        **legacy_kwargs: Any,
    ) -> None:
        if 'engine' in legacy_kwargs:
            raise TypeError(
                'WSQLExecutor(..., engine=...) is no longer supported because '
                'shared engines are not concurrency-safe. Configure crawler '
                'behavior with default_crawler_name and executor settings.'
            )
        if 'crawler' in legacy_kwargs:
            raise TypeError(
                'WSQLExecutor(..., crawler=...) is no longer supported. '
                'Use default_crawler_name for crawler selection.'
            )
        if legacy_kwargs:
            unknown = ', '.join(sorted(legacy_kwargs))
            raise TypeError(f'Unexpected keyword argument(s): {unknown}')

        self._default_crawler_name = (
            default_crawler_name.strip().lower()
            if isinstance(default_crawler_name, str) and default_crawler_name.strip()
            else None
        )
        self._concurrency = concurrency
        self._per_host = per_host
        self._respect_robots = respect_robots
        self._verify_ssl = verify_ssl
        self._headers = dict(headers) if headers else None
        self._playwright_stealth = playwright_stealth
        self._flaresolverr_url = flaresolverr_url
        self.yield_errors = yield_errors
        self._download_delay = (
            max(0.0, float(download_delay)) if download_delay is not None else None
        )

    def _resolve_crawler_name(self, crawler_name: str | None = None) -> str | None:
        """Resolve crawler name with expression value taking precedence."""
        if isinstance(crawler_name, str) and crawler_name.strip():
            return crawler_name.strip().lower()
        return self._default_crawler_name

    def _create_engine(
        self,
        crawler_name: str | None = None,
        from_clause: FromClause | None = None,
    ) -> WXPathEngine:
        """Create a fresh WXPathEngine instance.

        Args:
            crawler_name: Inline ``CRAWLER <name>`` override from the query,
                if any.
            from_clause: The originating ``FromClause``, used to validate
                that ``CLICK`` pagination is paired with the ``playwright``
                crawler.
        """
        resolved_crawler_name = self._resolve_crawler_name(crawler_name)
        if from_clause is not None:
            _validate_click_requires_playwright(from_clause, resolved_crawler_name)
        # Materialize an aiohttp crawler when download_delay is set so throttle
        # policy cannot silently fall back to wxpath AutoThrottler defaults.
        if resolved_crawler_name is None and self._download_delay is not None:
            resolved_crawler_name = 'aiohttp'
        crawler = self._create_crawler(resolved_crawler_name)
        log.debug('Creating WXPathEngine with crawler %s', crawler)

        return WXPathEngine(
            crawler=crawler,
            concurrency=self._concurrency,
            per_host=self._per_host,
            respect_robots=self._respect_robots,
        )

    def _create_crawler(self, crawler_name: str | None = None) -> Any | None:
        """Create a crawler instance or defer to engine defaults."""
        if crawler_name is None:
            return None

        crawler_class = _get_crawler_class(crawler_name)
        crawler_kwargs: dict[str, Any] = {
            'concurrency': self._concurrency,
            'per_host': self._per_host,
            'respect_robots': self._respect_robots,
            'verify_ssl': self._verify_ssl,
            'headers': self._headers,
        }
        if self._download_delay is not None:
            from wxpath.http.policy.throttler import FixedDelayThrottler, ImpoliteThrottle

            if self._download_delay > 0:
                crawler_kwargs['throttler'] = FixedDelayThrottler(self._download_delay)
            else:
                crawler_kwargs['throttler'] = ImpoliteThrottle()
        if crawler_name == 'playwright':
            crawler_kwargs['stealth'] = self._playwright_stealth
        elif crawler_name == 'flaresolverr':
            crawler_kwargs['flaresolverr_url'] = self._flaresolverr_url

        return crawler_class(**crawler_kwargs)

    async def execute(
        self,
        source: str | WSQLProgram,
        progress: bool = False,
        yield_errors: bool | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Execute a WSQL query and yield result dictionaries.

        Parses the source and selects execution strategy based on JOINs.

        Args:
            source: WSQL source string or pre-parsed ``WSQLProgram``.
            progress: If ``True``, show progress bar.
            yield_errors: If ``True``, yield error dictionaries.

        Yields:
            Result dictionaries from ``SELECT`` column evaluation.
        """
        log.info('Executing WSQL query %s', source)
        program = parse(source) if isinstance(source, str) else source
        yield_errors = yield_errors if yield_errors is not None else self.yield_errors

        if program.has_joins():
            # Horizontal execution: CTE rows -> main query fetches
            log.info('Executing with JOINs (horizontal mode)')
            async for result in self._execute_horizontal(program, progress, yield_errors):
                yield result
        else:
            # Simple execution: CTEs and main query are independent
            log.info('Executing without JOINs (simple mode)')
            async for result in self._execute_simple(program, progress, yield_errors):
                yield result

    async def _execute_simple(
        self,
        program: WSQLProgram,
        progress: bool,
        yield_errors: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Execute CTEs and main query independently (no JOINs).

        CTEs execute in parallel, then the main query runs.
        CTE results are not used by the main query.
        """
        # Execute CTEs in parallel (if any)
        if program.ctes:
            log.debug(f'Executing {len(program.ctes)} CTE(s) in parallel')
            cte_tasks = []
            for cte in program.ctes:
                task = asyncio.create_task(self._collect_cte(cte, progress, yield_errors))
                cte_tasks.append((cte.name, task))

            for cte_name, task in cte_tasks:
                rows = await task
                log.debug(f'CTE {cte_name} completed: {len(rows)} rows')

        # Execute main query
        log.debug('Executing main query %s', program)
        segments = transpile(program)
        max_depth = _get_effective_max_depth(program.main_query)
        crawler_name = program.main_query.from_clause.crawler_name
        engine = self._create_engine(crawler_name, program.main_query.from_clause)
        log.debug('Executing main query2 %s %s', segments, max_depth)
        async for output in _iter_engine_rows(
            engine.run(
                expression=segments,
                max_depth=max_depth,
                progress=progress,
                yield_errors=yield_errors,
            ),
            yield_errors=yield_errors,
        ):
            yield output

    async def _execute_horizontal(
        self,
        program: WSQLProgram,
        progress: bool,
        yield_errors: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Execute JOIN pipelines using a level-by-level dependency DAG.

        The executor computes a topological level order across queries and
        propagates row contexts through the graph:

        - Root queries (no dependencies) execute first.
        - Dependent queries execute only when provider rows exist in context.
        - ``main`` executes after its dependencies and yields final rows.

        Per-context query executions are run concurrently with bounded
        concurrency via ``asyncio.Semaphore``.
        """
        ctx = ExecutionContext(program)
        ctx._build_execution_context()

        # CTE-referenced columns each dependent query needs
        cte_cols_by_query: dict[str, list[tuple[str, str, str]]] = {}
        for query in program.get_all_queries():
            cte_cols = [
                (col.cte_field, col.alias, col.cte_name) for col in query.select if col.is_cte_ref
            ]
            if cte_cols:
                cte_cols_by_query[query.alias] = cte_cols

        required = _collect_required_queries(
            dependencies=ctx.dependencies,
            sink=program.main_query.alias,
        )
        query_by_name: dict[str, WSQLQuery] = {
            query.alias: query for query in program.get_all_queries() if query.alias in required
        }

        levels: list[list[str]] = [
            [name for name in level if name in required] for level in ctx.levels
        ]

        contexts_by_query: dict[str, list[dict[str, dict]]] = defaultdict(list)
        sem = asyncio.Semaphore(self._concurrency)
        pending: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        active_tasks = 0
        done_message = object()

        async def run_query_for_context(
            query_name: str,
            input_context: dict[str, dict],
        ) -> None:
            """Run one query for one input context and stream messages.

            Args:
                query_name: Query alias to execute.
                input_context: Context rows from provider queries.
            """
            try:
                async with sem:
                    query = query_by_name[query_name]
                    crawler_name = query.from_clause.crawler_name
                    segments = copy.deepcopy(ctx.query_name_to_segments[query_name])
                    _project_cte_context_values(segments, input_context)

                    max_depth = _get_effective_max_depth(query)
                    engine = self._create_engine(crawler_name, query.from_clause)
                    async for output in _iter_engine_rows(
                        engine.run(
                            segments,
                            max_depth,
                            progress=False,
                            yield_errors=yield_errors,
                        ),
                        yield_errors=yield_errors,
                    ):
                        row = output
                        if row.get('__type__') == 'error':
                            await pending.put(('error', row))
                            continue

                        merged = _merge_projected_columns(
                            row=row,
                            query_name=query_name,
                            context=input_context,
                            cte_cols_by_query=cte_cols_by_query,
                        )
                        out_context = dict(input_context)
                        out_context[query_name] = merged
                        await pending.put(('row', (query_name, out_context, merged)))
            except Exception as exc:
                await pending.put(('exception', exc))
            finally:
                await pending.put(('done', done_message))

        tasks: list[asyncio.Task[None]] = []
        try:
            for level in levels:
                for query_name in level:
                    providers = ctx.dependencies.get(query_name, [])
                    if not providers:
                        input_contexts = [{}]
                    else:
                        input_contexts = _combine_provider_contexts(
                            provider_names=providers,
                            contexts_by_query=contexts_by_query,
                        )

                    if not input_contexts:
                        continue

                    for input_context in input_contexts:
                        tasks.append(
                            asyncio.create_task(run_query_for_context(query_name, input_context))
                        )
                        active_tasks += 1

                while active_tasks > 0:
                    msg_type, payload = await pending.get()
                    if msg_type == 'done':
                        active_tasks -= 1
                        continue
                    if msg_type == 'exception':
                        raise payload
                    if msg_type == 'error':
                        yield payload
                        continue

                    node_name, out_context, merged = payload
                    contexts_by_query[node_name].append(out_context)
                    if node_name == program.main_query.alias:
                        yield merged
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _collect_cte(
        self,
        cte: CTE,
        progress: bool,
        yield_errors: bool,
    ) -> list[dict]:
        """Execute a CTE and collect all result rows."""
        rows, _ = await self._collect_cte_with_errors(cte, progress, yield_errors)
        return rows

    async def _collect_cte_with_errors(
        self,
        cte: CTE,
        progress: bool,
        yield_errors: bool,
    ) -> tuple[list[dict], list[dict]]:
        """Execute a CTE and return rows and errors separately."""
        segments = transpile_cte(cte)
        max_depth = _get_effective_max_depth(cte.query)
        crawler_name = cte.query.from_clause.crawler_name
        rows: list[dict] = []
        errors: list[dict] = []

        engine = self._create_engine(crawler_name, cte.query.from_clause)
        async for output in _iter_engine_rows(
            engine.run(
                expression=segments,
                max_depth=max_depth,
                progress=progress,
                yield_errors=True,
            ),
            yield_errors=True,
        ):
            if output.get('__type__') == 'error':
                errors.append(output)
            else:
                rows.append(output)

        return rows, errors

    async def execute_collect(
        self,
        source: str | WSQLProgram,
        progress: bool = False,
        yield_errors: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a WSQL query and return all results as a list."""
        results: list[dict[str, Any]] = []
        async for output in self.execute(source, progress, yield_errors):
            results.append(output)
        return results


# --------------------------------------------------------------------
#  Helper functions
# --------------------------------------------------------------------


async def _iter_engine_rows(
    engine_outputs: AsyncGenerator[Any, None],
    *,
    yield_errors: bool,
) -> AsyncGenerator[dict[str, Any], None]:
    """Normalize wxpath engine output into plain row dictionaries.

    Args:
        engine_outputs: Async iterator from ``WXPathEngine.run``.
        yield_errors: Whether to pass through error dictionaries.

    Yields:
        Normalized result rows and, when enabled, error dictionaries.
    """
    async for output in engine_outputs:
        if isinstance(output, dict) and output.get('__type__') == 'error':
            if yield_errors:
                yield output
            continue

        row = row_to_dict(output)
        if row is not None:
            yield row


def _collect_required_queries(
    dependencies: dict[str, list[str]],
    sink: str,
) -> set[str]:
    """Collect all queries required to produce results for *sink*.

    Args:
        dependencies: Query dependency map (consumer -> providers).
        sink: Target query alias (typically ``main``).

    Returns:
        Set containing *sink* and all transitive provider queries.
    """
    required: set[str] = set()
    stack = [sink]
    while stack:
        node = stack.pop()
        if node in required:
            continue
        required.add(node)
        stack.extend(dependencies.get(node, []))
    return required


def _combine_provider_contexts(
    provider_names: list[str],
    contexts_by_query: dict[str, list[dict[str, dict]]],
) -> list[dict[str, dict]]:
    """Build compatible input contexts across multiple providers.

    Args:
        provider_names: Query aliases that must be present.
        contexts_by_query: Materialized contexts keyed by provider alias.

    Returns:
        Compatible context combinations; empty when providers have no rows.
    """
    if not provider_names:
        return [{}]

    provider_contexts = [contexts_by_query.get(name, []) for name in provider_names]
    if any(not values for values in provider_contexts):
        return []

    combinations = provider_contexts[0]
    for next_contexts in provider_contexts[1:]:
        merged_contexts: list[dict[str, dict]] = []
        for left in combinations:
            for right in next_contexts:
                if _contexts_compatible(left, right):
                    merged_contexts.append({**left, **right})
        combinations = merged_contexts
        if not combinations:
            return []
    return combinations


def _contexts_compatible(left: dict[str, dict], right: dict[str, dict]) -> bool:
    """Check whether two row contexts can be merged.

    Args:
        left: Left context map.
        right: Right context map.

    Returns:
        ``True`` when shared aliases contain equal rows.
    """
    for key in set(left).intersection(right):
        if left[key] != right[key]:
            return False
    return True


def _merge_projected_columns(
    row: dict,
    query_name: str,
    context: dict[str, dict],
    cte_cols_by_query: dict[str, list[tuple[str, str, str]]],
) -> dict:
    """Merge CTE-referenced SELECT columns from context into output row.

    Args:
        row: Raw row produced by the query.
        query_name: Alias of the query producing *row*.
        context: Provider rows available for projection.
        cte_cols_by_query: Query-to-CTE column projection metadata.

    Returns:
        Output row with projected CTE column aliases populated.
    """
    merged = dict(row)
    for cte_field, alias, cte_name in cte_cols_by_query.get(query_name, []):
        provider_row = context.get(cte_name)
        if provider_row is not None:
            merged[alias] = provider_row.get(cte_field)
    return merged


def _project_cte_context_values(
    segments: list,
    context: dict[str, dict],
) -> None:
    """Project all provider values into a URL template segment.

    Args:
        segments: Query segment pipeline (modified in place).
        context: Provider rows keyed by query alias.
    """
    if not segments:
        return
    first_seg = segments[0]
    # NOTE: Projection is only supported for UrlCrawlWithProjection
    # TODO: Explore supporting other types of segments for projection
    if not isinstance(first_seg, UrlCrawlWithProjection):
        return
    for proj in first_seg.projection_metadata.expected_projections:
        if proj.cte_name in context:
            first_seg.projection_metadata.projected_values[proj.key()] = context[proj.cte_name].get(
                proj.cte_field
            )


def _project_cte_values(
    segments: list,
    parent_name: str,
    root_row: dict,
) -> None:
    """Write CTE field values from *root_row* into the URL template.

    Modifies ``segments[0]`` in place - callers must pass a deep copy.
    """
    if not segments:
        return
    first_seg = segments[0]
    if isinstance(first_seg, UrlCrawlWithProjection):
        for proj in first_seg.projection_metadata.expected_projections:
            if proj.cte_name == parent_name:
                first_seg.projection_metadata.projected_values[proj.key()] = root_row.get(
                    proj.cte_field
                )


class ExecutionContext:
    def __init__(self, program: WSQLProgram):
        self.program = program
        self.root_nodes: list[WSQLQuery] = []
        self.root_node: WSQLQuery | None = None
        self.root_node_name: str | None = None
        self.root_node_id: int | None = None
        self.dependencies: dict[str, list[str]] = {}
        self.dependents: dict[str, list[str]] = {}
        self.query_name_to_segments: dict[str, Segments] = {}
        self.levels: list[list[str]] = []

    @property
    def final_select_columns(self) -> list[str]:
        return self.program.main_query.select

    def _build_execution_context(self) -> None:
        """Build an execution DAG for a WSQL program."""
        self.dependencies, self.dependents = _build_dependencies(self.program)
        self.root_nodes = list(_iter_root_nodes(self.program, self.dependencies))
        if not self.root_nodes:
            raise ValueError('No root nodes found in execution graph')

        self.root_node = self.root_nodes[0]
        self.root_node_name = self.root_node.alias
        self.root_node_id = self.program.query_name_to_id[self.root_node_name]

        for query in self.program.get_all_queries():
            self.query_name_to_segments[query.alias] = _transpile_query(query)

        self.levels = self.topological_sort_with_levels(self.dependents)

    def topological_sort_with_levels(self, graph: dict[str, list[str]]) -> list[list[str]]:
        """
        Performs a topological sort on a DAG and groups nodes by level.

        Args:
            graph (dict): An adjacency list where keys are nodes and values are
                        lists of their neighbors (nodes they point to).

        Returns:
            list: A list of lists, where each inner list contains nodes at the
                same level of the topological order.
        """
        in_degree = defaultdict(int)
        # Ensure all nodes are in the in_degree dictionary, even those with no incoming edges
        for node in graph:
            for neighbor in graph[node]:
                in_degree[neighbor] += 1
            # Also ensure source nodes (which may not be destinations of any edge) are present
            if node not in in_degree:
                in_degree[node] = 0

        # Queue for nodes with zero in-degree (start of a level)
        queue = deque([node for node in in_degree if in_degree[node] == 0])
        levels = []

        while queue:
            # Process all nodes in the current level (nodes that can run in parallel)
            current_level_nodes = []
            for _ in range(len(queue)):
                node = queue.popleft()
                current_level_nodes.append(node)

                # Decrement the in-degree of neighbors
                for neighbor in graph.get(node, []):
                    in_degree[neighbor] -= 1
                    # If a neighbor's in-degree becomes zero, add it to the queue
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)

            levels.append(current_level_nodes)

        # Check for cycles
        total_nodes = len(in_degree)
        sorted_node_count = sum(len(level) for level in levels)
        if sorted_node_count != total_nodes:
            raise ValueError('Graph contains a cycle, topological sort is not possible.')

        return levels


# --------------------------------------------------------------------
#  Synchronous blocking API
# --------------------------------------------------------------------


def wsql_execute(
    source: str | WSQLProgram,
    progress: bool = False,
    executor: WSQLExecutor | None = None,
    yield_errors: bool = False,
) -> list[dict[str, Any]]:
    """Execute a WSQL query synchronously and return all results.

    This function creates its own event loop and must **not** be
    called from within an active asyncio event loop.

    Args:
        source: WSQL source string or pre-parsed ``WSQLProgram``.
        progress: Whether to show progress bar.
        executor: Optional pre-configured ``WSQLExecutor``.
        yield_errors: Whether to include error dictionaries.

    Returns:
        List of result dictionaries.

    Example::

        >>> results = wsql_execute(
        ...     "SELECT //h1/text() AS title FROM https://example.com DEPTH 1"
        ... )
        >>> results[0]['title']
        'Example Domain'
    """
    import asyncio

    if executor is None:
        executor = WSQLExecutor(yield_errors=yield_errors)

    previous_loop: asyncio.AbstractEventLoop | None
    try:
        previous_loop = asyncio.get_event_loop()
    except RuntimeError:
        previous_loop = None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        return loop.run_until_complete(executor.execute_collect(source, progress, yield_errors))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        if previous_loop is not None and not previous_loop.is_closed():
            asyncio.set_event_loop(previous_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())
