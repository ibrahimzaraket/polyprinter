"""Polygon RPC client for the CTF Exchange V2's `OrderFilled` event — Phase
4's on-chain detection path (FR-11).

Everything below was verified live 2026-08-08, not inferred from docs or
guessed from a name (see docs/PRD.md §9's "still genuinely open" item this
resolves):

- Contract address `0xE111180000d2663C0091e4f400237545B87B996B` is the
  CURRENT CTF Exchange V2 (docs.polymarket.com/resources/contracts —
  Polymarket migrated off the old `0x4bFb41d5...` address 2026-04-28).
- `OrderFilled(bytes32 orderHash, address maker, address taker, uint8 side,
  uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled,
  uint256 fee, bytes32 builder, bytes32 metadata)` — confirmed against the
  contract's own source (github.com/Polymarket/ctf-exchange-v2, src/
  exchange/mixins/Events.sol). orderHash/maker/taker are indexed (topics
  1-3); the rest is packed into `data` in that exact order.
- **The open question this resolves**: the contract emits TWO OrderFilled
  events per match — one describing the maker order, one describing the
  taker's own order — and in the taker's-own-order emission, `taker` is
  literally `address(this)` (the exchange contract), not a second real
  trader (confirmed in Trading.sol: every `_emitOrderFilledEvent`/
  `_emitTakerFilledEvents` call sets `maker` to a real order-owner address,
  never `address(this)`; `taker` is sometimes `address(this)`, sometimes
  real). **Always filter/match on `maker`, never `taker`** — every fill
  belonging to an address shows up there regardless of which side of the
  match they were on.
- `maker` IS the same proxy-wallet address data-api calls `proxyWallet` —
  no EOA/proxy translation needed. `tokenId` is the same value data-api
  calls `asset`. Both confirmed by decoding two real live transactions
  (one BUY, one SELL) and matching every field — maker address, tokenId,
  side, computed price/size — against `data-api`'s `/activity` response
  for the same `transactionHash`. USDC and CTF share amounts both use
  6-decimal scaling on-chain (confirmed against the same two transactions).
- making/taking convention: for a BUY, makerAmountFilled = USDC paid,
  takerAmountFilled = shares received. For a SELL, makerAmountFilled =
  shares given up, takerAmountFilled = USDC received. (Standard "you give
  what you're making, you get what you're taking" order-book convention;
  confirmed against both live examples above.)

No `web3.py` dependency — this only ever needs three plain JSON-RPC calls
(`eth_blockNumber`, `eth_getLogs`, `eth_getBlockByNumber`), which httpx
already covers, and one well-verified event shape to decode by hand.
Pulling in a whole SDK for that would be the "new dependency for no
reason" this project's own rules warn against.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from polyprinter.sources.raw_store import store_raw
from polyprinter.sources.retry import with_retry

CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
ORDER_FILLED_TOPIC0 = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
SOURCE = "polygon-rpc"
USDC_DECIMALS = 6
SHARE_DECIMALS = 6

BUY = 0
SELL = 1


def _pad_address(address: str) -> str:
    """Left-pads a 20-byte address to a 32-byte topic value, lowercased —
    the format eth_getLogs expects in a `topics` filter array.
    """
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _unpad_address(topic: str) -> str:
    return "0x" + topic[-40:]


class PolygonChainClient:
    def __init__(self, conn: sqlite3.Connection, *, rpc_url: str, timeout: float = 20.0):
        self.conn = conn
        self.rpc_url = rpc_url
        self._client = httpx.Client(timeout=timeout)
        self._block_ts_cache: dict[int, str] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PolygonChainClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _rpc(self, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call. Persisted raw (same structural rule as every
        other source) with the method+params folded into `url` — there's
        no natural per-call URL for a JSON-RPC POST endpoint, and encoding
        the request there keeps store_raw's (source, url, body) dedup key
        meaningful instead of every call sharing one bare URL.
        """
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        url = f"{self.rpc_url}?method={method}&params={params}"
        resp = with_retry(lambda: self._client.post(self.rpc_url, json=body))
        store_raw(self.conn, source=SOURCE, url=url, status=resp.status_code, body=resp.text)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{method} RPC error: {data['error']}")
        return data["result"]

    def latest_block(self) -> int:
        return int(self._rpc("eth_blockNumber", []), 16)

    def block_timestamp_iso(self, block_number: int) -> str:
        """Cached per client instance — a fetch cycle typically touches a
        handful of distinct blocks across many logs, not one per log.
        """
        if block_number not in self._block_ts_cache:
            from datetime import datetime, timezone

            block = self._rpc("eth_getBlockByNumber", [hex(block_number), False])
            ts = int(block["timestamp"], 16)
            self._block_ts_cache[block_number] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return self._block_ts_cache[block_number]

    def get_order_filled_logs(
        self, *, from_block: int, to_block: int, maker_addresses: list[str]
    ) -> list[dict[str, Any]]:
        """Raw (undecoded) OrderFilled logs for the CTF Exchange V2 where
        `maker` (topics[2]) is one of `maker_addresses` — server-side
        OR-matched via eth_getLogs' standard "array at one topic position
        means any-of" semantics (JSON-RPC spec, not Polymarket-specific).
        Empty `maker_addresses` returns [] without an RPC call — filtering
        on nothing would mean "match every maker," the opposite of intent.
        """
        if not maker_addresses:
            return []
        topics = [ORDER_FILLED_TOPIC0, None, [_pad_address(a) for a in maker_addresses]]
        return self._rpc(
            "eth_getLogs",
            [{"address": CTF_EXCHANGE_V2, "topics": topics, "fromBlock": hex(from_block), "toBlock": hex(to_block)}],
        )


def decode_order_filled_log(log: dict[str, Any]) -> dict[str, Any]:
    """Pure decode of one raw eth_getLogs entry into the fields Mirror
    cares about. No I/O — testable against fixed, real, live-captured logs
    (see tests/test_chain.py) without a network call or an RPC client.
    """
    maker = _unpad_address(log["topics"][2])
    taker = _unpad_address(log["topics"][3])

    data = log["data"].removeprefix("0x")
    words = [data[i : i + 64] for i in range(0, len(data), 64)]
    side, token_id, maker_amount_raw, taker_amount_raw, fee_raw = (int(w, 16) for w in words[:5])

    maker_amount = maker_amount_raw / 10**USDC_DECIMALS
    taker_amount = taker_amount_raw / 10**SHARE_DECIMALS
    if side == BUY:
        shares, usdc = taker_amount, maker_amount
    else:
        shares, usdc = maker_amount, taker_amount
    price = usdc / shares if shares else 0.0

    return {
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
        "block_number": int(log["blockNumber"], 16),
        "maker": maker,
        "taker": taker,
        "side": "BUY" if side == BUY else "SELL",
        "token_id": str(token_id),
        "shares": shares,
        "price": price,
        "fee_usd": fee_raw / 10**USDC_DECIMALS,
    }
