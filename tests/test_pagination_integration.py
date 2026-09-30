"""WSQL CLICK execution against wxpath's paginated response contract."""

from __future__ import annotations

import pytest
from wxpath.http.client.crawler import PlaywrightCrawler
from wxpath.http.client.request import Request
from wxpath.http.client.response import Response

from wsql.runtime import executor as executor_module
from wsql.runtime.executor import WSQLExecutor


class PaginatedCrawler(PlaywrightCrawler):
    """Provide two browser page responses without launching a browser."""

    requests: list[Request] = []

    async def __aenter__(self) -> PaginatedCrawler:
        return self

    async def __aexit__(self, *args) -> None:
        return None

    def submit(self, request: Request) -> None:
        self.requests.append(request)
        for number, name in enumerate(('one', 'two'), start=1):
            self._results.put_nowait(
                Response(
                    request,
                    200,
                    f'<html><body><li>{name}</li></body></html>',
                    page_url=f'https://example.test/page/{number}',
                    complete=number == 2,
                )
            )


@pytest.mark.asyncio
async def test_click_query_returns_rows_from_each_page(monkeypatch: pytest.MonkeyPatch) -> None:
    PaginatedCrawler.requests = []
    monkeypatch.setitem(
        executor_module.CRAWLER_NAME_TO_CRAWLER_CLASS, 'playwright', PaginatedCrawler
    )
    query = (
        'SELECT //li/text() AS name FROM https://example.test/page/1 '
        "CLICK //a[@id='next'] PAGES 2 CRAWLER playwright"
    )

    rows = await WSQLExecutor().execute_collect(query)

    assert rows == [{'name': 'one'}, {'name': 'two'}]
    assert len(PaginatedCrawler.requests) == 1
    assert PaginatedCrawler.requests[0].pagination.click_xpath == "//a[@id='next']"
    assert PaginatedCrawler.requests[0].pagination.max_pages == 2
