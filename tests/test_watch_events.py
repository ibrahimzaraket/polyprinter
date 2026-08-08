"""watch_events.py against a real (temp-file) db and a fake chain client
(no network, no real RPC). Focused on the two things that matter for the
diff-harness stage: checkpoint advancement (no re-processing, no gaps) and
that this never touches observed_trades/decisions (see the module
docstring for why that's the point, not an oversight).
"""

from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.mirror import watch_events
from polyprinter.obs.log import Logger
from tests.test_chain import BUY_LOG, SELL_LOG

ADDRESS = "0x335ac48637a3bc37c2eac9e0c9799c32b3cda494"


class FakeChainClient:
    """Stands in for PolygonChainClient — same call shape, serves fixed
    data regardless of the block range asked for (process_range only ever
    asks for one range per call in these tests).
    """

    def __init__(self, *, latest: int, logs: list[dict]):
        self.latest = latest
        self.logs = logs
        self.get_logs_calls: list[tuple[int, int]] = []

    def latest_block(self) -> int:
        return self.latest

    def get_order_filled_logs(self, *, from_block, to_block, maker_addresses):
        self.get_logs_calls.append((from_block, to_block))
        return list(self.logs)

    def block_timestamp_iso(self, block_number: int) -> str:
        return datetime.now(timezone.utc).isoformat()


def _make_db(tmp_path):
    return get_connection(tmp_path / "test.db")


def _log(conn):
    return Logger("test", conn)


def test_first_run_anchors_behind_the_tip_not_from_genesis(tmp_path):
    conn = _make_db(tmp_path)
    client = FakeChainClient(latest=1000, logs=[])

    n = watch_events.process_range(conn, _log(conn), client, watchlist=[ADDRESS], confirmations=5)

    assert n == 0
    assert client.get_logs_calls == [(1000 - 5 - watch_events.DEFAULT_LOOKBACK_BLOCKS, 1000 - 5)]


def test_detected_fill_is_logged_not_written_to_observed_trades(tmp_path):
    conn = _make_db(tmp_path)
    client = FakeChainClient(latest=1000, logs=[BUY_LOG])

    n = watch_events.process_range(conn, _log(conn), client, watchlist=[ADDRESS], confirmations=5)

    assert n == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM observed_trades").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 0
    detected = conn.execute(
        "SELECT context_json FROM events WHERE message = ?", (watch_events.DETECTED_MESSAGE,)
    ).fetchall()
    assert len(detected) == 1


def test_second_run_resumes_from_checkpoint_not_from_scratch(tmp_path):
    conn = _make_db(tmp_path)
    client1 = FakeChainClient(latest=1000, logs=[BUY_LOG])
    watch_events.process_range(conn, _log(conn), client1, watchlist=[ADDRESS], confirmations=5)

    client2 = FakeChainClient(latest=1010, logs=[SELL_LOG])
    watch_events.process_range(conn, _log(conn), client2, watchlist=[ADDRESS], confirmations=5)

    # second call should start right after the first call's target block
    assert client2.get_logs_calls == [(1000 - 5 + 1, 1010 - 5)]
    detected = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE message = ?", (watch_events.DETECTED_MESSAGE,)
    ).fetchone()["n"]
    assert detected == 2


def test_no_new_confirmed_blocks_is_a_noop(tmp_path):
    conn = _make_db(tmp_path)
    client1 = FakeChainClient(latest=1000, logs=[])
    watch_events.process_range(conn, _log(conn), client1, watchlist=[ADDRESS], confirmations=5)

    client2 = FakeChainClient(latest=1000, logs=[BUY_LOG])  # tip hasn't advanced past what's already confirmed
    n = watch_events.process_range(conn, _log(conn), client2, watchlist=[ADDRESS], confirmations=5)

    assert n == 0
    assert client2.get_logs_calls == []
