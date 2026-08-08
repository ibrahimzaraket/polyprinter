import pytest

from polyprinter.mirror.fills import BookLevel, InsufficientDepth, walk_book


def test_walk_book_single_level_fill():
    book = [BookLevel(price=0.50, size=1000)]
    fill = walk_book(book, size_usd=100, fee_bps=0)
    assert fill.shares == pytest.approx(200)  # $100 / $0.50
    assert fill.avg_price == pytest.approx(0.50)
    assert fill.cost_usd == pytest.approx(100)
    assert fill.fee_usd == 0
    assert fill.total_usd == pytest.approx(100)


def test_walk_book_crosses_multiple_levels():
    book = [BookLevel(price=0.50, size=100), BookLevel(price=0.55, size=1000)]
    # first $50 fills at 0.50 (100 shares), remaining $50 fills at 0.55
    fill = walk_book(book, size_usd=100, fee_bps=0)
    assert fill.cost_usd == pytest.approx(100)
    # avg price should sit between the two levels, weighted toward more shares at the cheaper level
    assert 0.50 < fill.avg_price < 0.55


def test_walk_book_applies_fee():
    book = [BookLevel(price=0.50, size=1000)]
    fill = walk_book(book, size_usd=100, fee_bps=200)  # 2%
    assert fill.fee_usd == pytest.approx(2.0)
    assert fill.total_usd == pytest.approx(102.0)


def test_walk_book_raises_on_insufficient_depth():
    book = [BookLevel(price=0.50, size=10)]  # only $5 worth
    with pytest.raises(InsufficientDepth):
        walk_book(book, size_usd=100)


def test_walk_book_rejects_non_positive_size():
    with pytest.raises(ValueError):
        walk_book([BookLevel(price=0.5, size=10)], size_usd=0)
