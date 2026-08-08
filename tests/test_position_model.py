from polyprinter.mirror.position_model import TradeLeg, fraction_of, running_position


def test_running_position_accumulates_buys():
    events = [TradeLeg("BUY", 100), TradeLeg("BUY", 50)]
    assert running_position(events) == 150


def test_running_position_subtracts_sells():
    events = [TradeLeg("BUY", 100), TradeLeg("SELL", 40)]
    assert running_position(events) == 60


def test_running_position_empty_is_zero():
    assert running_position([]) == 0.0


def test_fraction_of_basic():
    # they held 100, sold 40 -> 40% of their holding
    assert fraction_of(100.0, 40.0) == 0.4


def test_fraction_of_full_exit():
    assert fraction_of(100.0, 100.0) == 1.0


def test_fraction_of_clamps_over_100_percent():
    # float drift / an incomplete observed history could put this over 1.0
    assert fraction_of(100.0, 110.0) == 1.0


def test_fraction_of_none_when_no_prior_position():
    assert fraction_of(None, 40.0) is None


def test_fraction_of_none_when_prior_position_not_positive():
    assert fraction_of(0.0, 40.0) is None
    assert fraction_of(-5.0, 40.0) is None
