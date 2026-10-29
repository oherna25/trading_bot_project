from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from bot.config import read_dotenv as _read_dotenv
from bot.feeds import TRADIER_SANDBOX_URL
from bot.risk import is_options_leg
from bot.shapes import OrderIntent

HTTP_TIMEOUT_SECONDS = 30.0

HttpPostForm = Callable[..., Any]
HttpGetJson = Callable[..., Any]


class LiveConfigError(ValueError):
    pass


class LiveOrderError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveAck:
    order_id: str
    status: str
    fill_price: float | None
    quantity: int


def occ_symbol(underlying: str, expiry: str, call_or_put: str, strike: float) -> str:
    root = underlying.strip().upper()[:6].ljust(6)
    try:
        parsed = datetime.fromisoformat(expiry).date()
    except ValueError:
        raise LiveOrderError(f"bad option expiry {expiry!r}") from None
    if call_or_put not in ("call", "put"):
        raise LiveOrderError(f"bad call_or_put {call_or_put!r}")
    if strike <= 0:
        raise LiveOrderError(f"bad option strike {strike!r}")
    flag = "C" if call_or_put == "call" else "P"
    return f"{root}{parsed.strftime('%y%m%d')}{flag}{int(round(strike * 1000)):08d}"


def _urllib_post_form(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    data = urllib.parse.urlencode({k: str(v) for k, v in payload.items()}).encode("utf-8")
    merged = dict(headers)
    merged["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=merged)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _urllib_get_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass(frozen=True)
class TradierVenue:
    account_id: str
    token: str
    base_url: str = TRADIER_SANDBOX_URL
    order_type: str = "market"
    http_post: HttpPostForm = field(default=_urllib_post_form, repr=False)
    http_get: HttpGetJson = field(default=_urllib_get_json, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TradierVenue:
        if env is None:
            file_values = _read_dotenv()
            env = {
                "TRADIER_ACCESS_TOKEN": os.environ.get("TRADIER_ACCESS_TOKEN", file_values.get("TRADIER_ACCESS_TOKEN", "")),
                "TRADIER_ACCOUNT_ID": os.environ.get("TRADIER_ACCOUNT_ID", file_values.get("TRADIER_ACCOUNT_ID", "")),
                "TRADIER_BASE_URL": os.environ.get("TRADIER_BASE_URL", file_values.get("TRADIER_BASE_URL", "")),
            }
        token = (env.get("TRADIER_ACCESS_TOKEN") or "").strip()
        account_id = (env.get("TRADIER_ACCOUNT_ID") or "").strip()
        if not token:
            raise LiveConfigError("missing TRADIER_ACCESS_TOKEN for live venue")
        if not account_id:
            raise LiveConfigError("missing TRADIER_ACCOUNT_ID for live venue")
        base_url = (env.get("TRADIER_BASE_URL") or "").strip() or TRADIER_SANDBOX_URL
        return cls(account_id=account_id, token=token, base_url=base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _place_and_verify(
        self,
        *,
        symbol: str,
        order_class: str,
        side: str,
        quantity: int,
        reference_price: float,
    ) -> LiveAck:
        payload: dict[str, Any] = {
            "class": order_class,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "type": self.order_type,
            "duration": "day",
        }
        if self.order_type == "limit":
            payload["price"] = reference_price
        placed = self.http_post(
            f"{self.base_url}/accounts/{self.account_id}/orders",
            self._headers(),
            payload,
            HTTP_TIMEOUT_SECONDS,
        )
        order_id = str((placed.get("order") or {}).get("id") or "")
        if not order_id:
            raise LiveOrderError("broker returned no order id")
        status_body = self.http_get(
            f"{self.base_url}/accounts/{self.account_id}/orders/{order_id}",
            self._headers(),
            HTTP_TIMEOUT_SECONDS,
        )
        node = status_body.get("order") or {}
        status = str(node.get("status") or "")
        if status != "filled":
            raise LiveOrderError(f"order {order_id} not filled: {status or 'unknown'}; manage it manually")
        try:
            fill_price = float(node.get("avg_fill_price"))
        except (TypeError, ValueError):
            raise LiveOrderError(f"order {order_id} filled without a price") from None
        if fill_price <= 0:
            raise LiveOrderError(f"order {order_id} filled without a price")
        return LiveAck(order_id=order_id, status=status, fill_price=fill_price, quantity=quantity)

    def _place_combo(self, intent: OrderIntent, reference_price: float) -> LiveAck:
        for index, leg in enumerate(intent.legs):
            if not is_options_leg(leg):
                raise LiveOrderError(
                    f"combo leg {index + 1} ({leg.symbol} {leg.side}) is not an option leg"
                )
        specs = [
            (
                occ_symbol(leg.symbol, leg.expiry, leg.call_or_put, leg.strike or 0.0),
                leg.side,
                max(1, int(leg.size // (reference_price * 100.0))),
            )
            for leg in intent.legs
        ]
        order_ids: list[str] = []
        net_premium = 0.0
        total_quantity = 0
        for index, (symbol, side, quantity) in enumerate(specs):
            try:
                ack = self._place_and_verify(
                    symbol=symbol,
                    order_class="option",
                    side=side,
                    quantity=quantity,
                    reference_price=reference_price,
                )
            except LiveOrderError as exc:
                filled = ", ".join(order_ids) if order_ids else "none"
                raise LiveOrderError(
                    f"combo leg {index + 1}/{len(specs)} ({side} {symbol}) failed: {exc}; "
                    f"filled orders [{filled}] need manual management"
                ) from None
            order_ids.append(ack.order_id)
            total_quantity += ack.quantity
            net_premium += ack.fill_price if side == "buy" else -ack.fill_price
        return LiveAck(
            order_id="+".join(order_ids),
            status="filled",
            fill_price=net_premium,
            quantity=total_quantity,
        )

    def place_order(self, intent: OrderIntent, reference_price: float) -> LiveAck:
        if reference_price <= 0:
            raise LiveOrderError("live order needs a positive reference price for sizing")
        if intent.legs:
            if len(intent.legs) > 1:
                return self._place_combo(intent, reference_price)
            leg = intent.legs[0]
            if not is_options_leg(leg):
                raise LiveOrderError("live venue takes one equity order or one option leg")
            symbol = occ_symbol(leg.symbol, leg.expiry, leg.call_or_put, leg.strike or 0.0)
            quantity = max(1, int(leg.size // (reference_price * 100.0)))
            return self._place_and_verify(
                symbol=symbol,
                order_class="option",
                side=leg.side,
                quantity=quantity,
                reference_price=reference_price,
            )
        quantity = max(1, int(intent.size // reference_price))
        return self._place_and_verify(
            symbol=intent.symbol,
            order_class="equity",
            side=intent.side,
            quantity=quantity,
            reference_price=reference_price,
        )
