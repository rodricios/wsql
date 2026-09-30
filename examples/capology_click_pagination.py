"""Run the Capology click-pagination demo.

Extracts the Premier League salary table from capology.com by clicking
through the "next page" control on a live Playwright page, re-extracting
after every click, rather than following a fetchable link.
"""

from __future__ import annotations

from pathlib import Path

from wsql import WSQLExecutor, wsql_execute

QUERY_PATH = Path(__file__).with_name('capology_click_pagination.wsql')


def main() -> None:
    """Execute the demo query and print a short summary per row."""
    query = QUERY_PATH.read_text(encoding='utf-8')

    # CLICK pagination requires a live browser page. The query already
    # sets CRAWLER playwright inline, but default_crawler_name is set
    # here too so the same executor works if that line is ever removed.
    executor = WSQLExecutor(default_crawler_name='playwright')
    results = wsql_execute(query, executor=executor)

    if not results:
        print('No results returned.')
        return

    print(f'Extracted {len(results)} salary rows across paginated table.')
    for index, row in enumerate(results[:10], start=1):
        name = row.get('name', '<unknown>')
        salary = row.get('annual_salary', '<unknown>')
        print(f'{index}. {name} - {salary}')


if __name__ == '__main__':
    main()
