"""
Pagination utilities for inline keyboard lists.

20 records per page by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar, Generic, Sequence

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20


@dataclass
class Page(Generic[T]):
    """A page of results with navigation info."""
    items: Sequence[T]
    page: int          # Current page (1-indexed)
    total_pages: int
    total_items: int
    has_prev: bool
    has_next: bool

    @property
    def is_empty(self) -> bool:
        return len(self.items) == 0


def paginate(
    items: Sequence[T],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page[T]:
    """
    Paginate a sequence of items.

    Args:
        items: Full list of items
        page: 1-indexed page number
        page_size: Items per page
    """
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size

    return Page(
        items=items[start:end],
        page=page,
        total_pages=total_pages,
        total_items=total,
        has_prev=page > 1,
        has_next=page < total_pages,
    )


def calc_offset(page: int, page_size: int = DEFAULT_PAGE_SIZE) -> int:
    """Calculate SQL OFFSET from page number."""
    return (max(1, page) - 1) * page_size
