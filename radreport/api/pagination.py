"""Pages for every list endpoint: a bounded LIMIT/OFFSET query, and the totals in response headers.

Order: describe the page asked for (Page.of) -> run the query for that page only, plus a count
(paginate) -> tell the caller where they are (headers: X-Total-Count, X-Page, X-Page-Size, Link).
List bodies stay plain JSON arrays, so existing callers keep working and read page 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Response, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class Page:
    number: int = 1
    size: int = DEFAULT_PAGE_SIZE

    @classmethod
    def of(cls, page: int | None, page_size: int | None) -> Page:
        """A validated page; the access policy already refuses non-numbers, this refuses out-of-range ones."""
        number, size = page or 1, page_size or DEFAULT_PAGE_SIZE
        if number < 1 or not 1 <= size <= MAX_PAGE_SIZE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"page must be 1 or more and page_size between 1 and {MAX_PAGE_SIZE}")
        return cls(number=number, size=size)

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size


@dataclass(frozen=True, slots=True)
class Paged:
    rows: list[Any]
    total: int
    page: Page

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page.size))


def paginate(session: Session, query: Select[Any], page: Page, *, scalars: bool = True) -> Paged:
    """Run `query` for one page, and count the whole result."""
    total = session.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    limited = query.limit(page.size).offset(page.offset)
    result = session.execute(limited)
    rows = list(result.scalars().all()) if scalars else list(result.all())
    return Paged(rows=rows, total=int(total), page=page)


async def paginate_async(session: Any, query: Select[Any], page: Page, *, scalars: bool = True) -> Paged:
    """`paginate` for an AsyncSession."""
    total = (await session.execute(select(func.count()).select_from(query.order_by(None).subquery()))).scalar_one()
    result = await session.execute(query.limit(page.size).offset(page.offset))
    rows = list(result.scalars().all()) if scalars else list(result.all())
    return Paged(rows=rows, total=int(total), page=page)


def paginate_list(items: list[Any], page: Page) -> Paged:
    """The same for a list already built in memory, when the ordering is computed in Python."""
    return Paged(rows=items[page.offset : page.offset + page.size], total=len(items), page=page)


def set_page_headers(response: Response, paged: Paged, path: str, extra: dict[str, str] | None = None) -> None:
    """Totals and RFC 8288 next/prev links."""
    response.headers["X-Total-Count"] = str(paged.total)
    response.headers["X-Page"] = str(paged.page.number)
    response.headers["X-Page-Size"] = str(paged.page.size)
    links = []
    base = dict(extra or {})
    if paged.page.number < paged.pages:
        links.append(f'<{path}?{urlencode({**base, "page": paged.page.number + 1, "page_size": paged.page.size})}>; rel="next"')
    if paged.page.number > 1:
        links.append(f'<{path}?{urlencode({**base, "page": paged.page.number - 1, "page_size": paged.page.size})}>; rel="prev"')
    if links:
        response.headers["Link"] = ", ".join(links)
