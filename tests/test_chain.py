"""decode_order_filled_log() against real, live-captured eth_getLogs
entries (2026-08-08) — every decoded field was cross-checked against
data-api's /activity response for the same transactionHash before being
frozen here as a fixture. See sources/chain.py's module docstring for the
full verification writeup. No network call, no RPC client — pure decode.

Fixtures below are byte-exact copies of real eth_getLogs/eth_getTransactionReceipt
responses (via https://polygon-bor-rpc.publicnode.com), not hand-transcribed.
"""

import pytest

from polyprinter.sources.chain import decode_order_filled_log

BUY_LOG = {
    "address": "0xe111180000d2663c0091e4f400237545b87b996b",
    "topics": [
        "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",
        "0xb3848d1ad8c72a4d437da2317f44a6be77bfc2588a3f652c9bf21b663542347d",
        "0x000000000000000000000000335ac48637a3bc37c2eac9e0c9799c32b3cda494",
        "0x000000000000000000000000686024b5c1fcc10eb86b8d551fae78488b1b6279"
    ],
    "data": "0x00000000000000000000000000000000000000000000000000000000000000003af5e0df554714ab5502af32084f9ed36e1910366284a76d464ff8af8603ed5e000000000000000000000000000000000000000000000000000000000023f0000000000000000000000000000000000000000000000000000000000000271000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
    "blockNumber": "0x576a000",
    "transactionHash": "0x82ea0e5a13f6365049fb1ed1dd1dad91adc954f6116c031862f5a72f817c5686",
    "transactionIndex": "0x40",
    "blockHash": "0xf5955484ad165d8269b7d6d40e0866c94148f8188d5408b23a366a6af6f8a852",
    "blockTimestamp": "0x6a772e03",
    "logIndex": "0x18a",
    "removed": False
}

TAKER_OWN_ORDER_LOG = {
    "address": "0xe111180000d2663c0091e4f400237545b87b996b",
    "topics": [
        "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",
        "0x85c2f51ac84ee738746091a416e27427d74258f83bae0ca60564aa9c4febf3d1",
        "0x000000000000000000000000686024b5c1fcc10eb86b8d551fae78488b1b6279",
        "0x000000000000000000000000e111180000d2663c0091e4f400237545b87b996b"
    ],
    "data": "0x00000000000000000000000000000000000000000000000000000000000000013af5e0df554714ab5502af32084f9ed36e1910366284a76d464ff8af8603ed5e0000000000000000000000000000000000000000000000000000000000271000000000000000000000000000000000000000000000000000000000000023f000000000000000000000000000000000000000000000000000000000000000337c00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
    "blockNumber": "0x576a000",
    "transactionHash": "0x82ea0e5a13f6365049fb1ed1dd1dad91adc954f6116c031862f5a72f817c5686",
    "transactionIndex": "0x40",
    "blockHash": "0xf5955484ad165d8269b7d6d40e0866c94148f8188d5408b23a366a6af6f8a852",
    "blockTimestamp": "0x6a772e03",
    "logIndex": "0x18d",
    "removed": False
}

SELL_LOG = {
    "address": "0xe111180000d2663c0091e4f400237545b87b996b",
    "topics": [
        "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",
        "0xcf027caa648e3784dbf06cbe427e99d45d6189cc2464da8815a7ac2a8badf4ca",
        "0x000000000000000000000000335ac48637a3bc37c2eac9e0c9799c32b3cda494",
        "0x000000000000000000000000e111180000d2663c0091e4f400237545b87b996b"
    ],
    "data": "0x0000000000000000000000000000000000000000000000000000000000000001f5b1a1fcb93242ebde61172246439f092197a3b5040040736b1ea503edcdb03e00000000000000000000000000000000000000000000000000000000005b8d8000000000000000000000000000000000000000000000000000000000002bf200000000000000000000000000000000000000000000000000000000000001997e00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
    "blockNumber": "0x576a150",
    "transactionHash": "0x4137856c9f4b7b4fdd488c986527d5eacca02997477d5cb07a170db25e7480d9",
    "transactionIndex": "0x76",
    "blockHash": "0x857ce3d54b374a139a98cbe054de34a72aa0b8fb70ef4462e84a132a65fd7a0a",
    "blockTimestamp": "0x6a772ffb",
    "logIndex": "0x546",
    "removed": False
}


def test_decode_buy_matches_data_api():
    """tx 0x82ea0e5a... verified against data-api /activity: side BUY,
    asset (tokenId) matches, size 2.56, usdcSize 2.3552, price 0.92."""
    decoded = decode_order_filled_log(BUY_LOG)
    assert decoded["maker"] == "0x335ac48637a3bc37c2eac9e0c9799c32b3cda494"
    assert decoded["taker"] == "0x686024b5c1fcc10eb86b8d551fae78488b1b6279"
    assert decoded["side"] == "BUY"
    assert decoded["token_id"] == "26668574760930728259231314028185687024922326310580986080795483281195870186846"
    assert decoded["shares"] == pytest.approx(2.56)
    assert decoded["price"] == pytest.approx(0.92)
    assert decoded["tx_hash"] == "0x82ea0e5a13f6365049fb1ed1dd1dad91adc954f6116c031862f5a72f817c5686"
    assert decoded["log_index"] == 0x18A


def test_decode_takers_own_order_maker_field_is_the_real_trader():
    """The PRD's open question, made concrete: this log's `maker` must be
    the real trader who placed the taker order, and `taker` must be the
    exchange contract itself — never mistake the second for a person.
    """
    decoded = decode_order_filled_log(TAKER_OWN_ORDER_LOG)
    assert decoded["maker"] == "0x686024b5c1fcc10eb86b8d551fae78488b1b6279"
    assert decoded["taker"] == "0xe111180000d2663c0091e4f400237545b87b996b"  # the exchange, not a trader


def test_decode_sell_matches_data_api():
    """tx 0x4137856c... verified against data-api /activity: side SELL,
    size 6, price 0.48 (== 2.88 usdc / 6 shares)."""
    decoded = decode_order_filled_log(SELL_LOG)
    assert decoded["maker"] == "0x335ac48637a3bc37c2eac9e0c9799c32b3cda494"
    assert decoded["side"] == "SELL"
    assert decoded["shares"] == pytest.approx(6.0)
    assert decoded["price"] == pytest.approx(0.48)
    assert decoded["fee_usd"] == pytest.approx(0.10483)
