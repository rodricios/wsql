"""WSQL - SQL-like syntax for web crawling and extraction.

Built on top of wxpath, WSQL provides a declarative SQL-like interface
for extracting structured data from web pages.

Example::

    >>> from wsql import wsql_execute
    >>> results = wsql_execute('''
    ...     SELECT //h1/text() AS title
    ...     FROM https://example.com
    ...     DEPTH 1
    ... ''')
    >>> results[0]['title']
    'Example Domain'
"""

# Import ops to register operators with wxpath's OPS_REGISTER.
# This must happen before any execution occurs.
import wsql.core.ops  # noqa: F401
from wsql.core.parser import parse, transpile, transpile_cte
from wsql.runtime.executor import WSQLExecutor, wsql_execute

__all__ = [
    'parse',
    'transpile',
    'transpile_cte',
    'WSQLExecutor',
    'wsql_execute',
]
