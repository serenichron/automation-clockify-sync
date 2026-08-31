#!/usr/bin/env python3
"""Produce audit-safe USD currency summaries from ECB daily reference rates."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = "fx-quote-receipt/v1"
CENT = Decimal("0.01")
MAX_QUOTE_AGE_DAYS = 4
DEFAULT_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class CurrencyContractError(ValueError):
    """Currency evidence is incomplete, stale, or internally inconsistent."""


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise CurrencyContractError(f"{field} must be a Decimal")
    if not value.is_finite():
        raise CurrencyContractError(f"{field} must be a finite Decimal")
    return value


def _currency(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise CurrencyContractError(f"{field} must be an uppercase ISO currency code")
    return value


def _receipt_decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise CurrencyContractError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CurrencyContractError(f"{field} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CurrencyContractError(f"{field} must be nonnegative and finite")
    return parsed


@dataclass(frozen=True)
class FxQuoteReceipt:
    provider: str
    effective_date: date
    fetched_at: datetime
    base_currency: str
    rates: Mapping[str, Decimal]
    payload_digest: str

    def __post_init__(self) -> None:
        if self.provider != "ECB":
            raise CurrencyContractError("quote provider must be ECB")
        if not isinstance(self.effective_date, date) or isinstance(self.effective_date, datetime):
            raise CurrencyContractError("quote effective date is required")
        if not isinstance(self.fetched_at, datetime) or self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise CurrencyContractError("quote fetched_at must include a timezone")
        if self.base_currency != "EUR":
            raise CurrencyContractError("quote base currency must be EUR")
        if not isinstance(self.rates, Mapping) or not self.rates:
            raise CurrencyContractError("quote rates are required")
        normalized: dict[str, Decimal] = {}
        for code, rate in self.rates.items():
            normalized[_currency(code, "quote rate currency")] = _decimal(rate, "quote rate")
            if normalized[code] <= 0:
                raise CurrencyContractError("quote rate must be positive")
        if "USD" not in normalized:
            raise CurrencyContractError("quote requires a USD rate")
        if not isinstance(self.payload_digest, str) or not _DIGEST.fullmatch(self.payload_digest):
            raise CurrencyContractError("quote payload digest must be sha256")
        object.__setattr__(self, "rates", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "provider": self.provider,
            "effective_date": self.effective_date.isoformat(),
            "fetched_at": self.fetched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "base_currency": self.base_currency,
            "rates": {code: format(rate, "f") for code, rate in sorted(self.rates.items())},
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class CurrencySummary:
    native_buckets: Mapping[str, Decimal]
    usd_buckets: Mapping[str, Decimal]
    usd_equivalent_total: Decimal

    def __post_init__(self) -> None:
        native = _validate_buckets(self.native_buckets, "native bucket")
        usd = _validate_buckets(self.usd_buckets, "USD bucket", require_cent=True)
        if set(native) != set(usd):
            raise CurrencyContractError("USD buckets must match native buckets")
        total = _decimal(self.usd_equivalent_total, "USD equivalent total")
        if total != total.quantize(CENT):
            raise CurrencyContractError("USD equivalent total must be rounded to cents")
        recomputed = sum(usd.values(), Decimal("0")).quantize(CENT)
        if total != recomputed:
            raise CurrencyContractError("USD equivalent total does not match buckets")
        object.__setattr__(self, "native_buckets", native)
        object.__setattr__(self, "usd_buckets", usd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "native_buckets": {code: format(value, "f") for code, value in sorted(self.native_buckets.items())},
            "usd_buckets": {code: format(value, "f") for code, value in sorted(self.usd_buckets.items())},
            "usd_equivalent_total": format(self.usd_equivalent_total, "f"),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, quote: FxQuoteReceipt, publication_date: date
    ) -> CurrencySummary:
        if not isinstance(value, Mapping) or set(value) != {"native_buckets", "usd_buckets", "usd_equivalent_total"}:
            raise CurrencyContractError("currency summary has unexpected fields")
        supplied = cls(
            native_buckets=_deserialize_buckets(value["native_buckets"], "native bucket"),
            usd_buckets=_deserialize_buckets(value["usd_buckets"], "USD bucket"),
            usd_equivalent_total=_receipt_decimal(value["usd_equivalent_total"], "USD equivalent total"),
        )
        expected = convert_native_buckets(
            supplied.native_buckets, quote, publication_date=publication_date
        )
        if supplied.usd_buckets != expected.usd_buckets:
            raise CurrencyContractError("USD buckets do not match the FX quote")
        if supplied.usd_equivalent_total != expected.usd_equivalent_total:
            raise CurrencyContractError("USD equivalent total does not match the FX quote")
        return supplied


def _validate_buckets(value: Any, field: str, *, require_cent: bool = False) -> dict[str, Decimal]:
    if not isinstance(value, Mapping) or not value:
        raise CurrencyContractError(f"{field}s are required")
    result: dict[str, Decimal] = {}
    for code, amount in value.items():
        normalized = _currency(code, f"{field} currency")
        parsed = _decimal(amount, field)
        if parsed < 0:
            raise CurrencyContractError(f"{field} must be nonnegative")
        if require_cent and parsed != parsed.quantize(CENT):
            raise CurrencyContractError(f"{field} must be rounded to cents")
        result[normalized] = parsed
    return result


def _deserialize_buckets(value: Any, field: str) -> dict[str, Decimal]:
    if not isinstance(value, Mapping):
        raise CurrencyContractError(f"{field}s are required")
    return {_currency(code, f"{field} currency"): _receipt_decimal(amount, field) for code, amount in value.items()}


def _to_usd(amount: Decimal, currency: str, quote: FxQuoteReceipt) -> Decimal:
    if currency == "USD":
        raw = amount
    elif currency == "EUR":
        raw = amount * quote.rates["USD"]
    else:
        try:
            raw = amount / quote.rates[currency] * quote.rates["USD"]
        except KeyError as exc:
            raise CurrencyContractError(f"quote rate is missing for {currency}") from exc
    return raw.quantize(CENT, rounding=ROUND_HALF_UP)


def convert_native_buckets(
    native_buckets: Mapping[str, Decimal], quote: FxQuoteReceipt, *, publication_date: date
) -> CurrencySummary:
    if not isinstance(quote, FxQuoteReceipt):
        raise CurrencyContractError("quote is required")
    if not isinstance(publication_date, date) or isinstance(publication_date, datetime):
        raise CurrencyContractError("publication date is required")
    age = (publication_date - quote.effective_date).days
    if age < 0:
        raise CurrencyContractError("quote effective date is after publication date")
    if age > MAX_QUOTE_AGE_DAYS:
        raise CurrencyContractError("quote is stale")
    native = _validate_buckets(native_buckets, "native bucket")
    usd = {code: _to_usd(amount, code, quote) for code, amount in native.items()}
    return CurrencySummary(native, usd, sum(usd.values(), Decimal("0")).quantize(CENT))


def parse_ecb_quote(payload: bytes, *, publication_date: date, fetched_at: datetime) -> FxQuoteReceipt:
    if not isinstance(payload, bytes) or not payload:
        raise CurrencyContractError("ECB response is required")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise CurrencyContractError("ECB response is not valid XML") from exc
    candidates: list[tuple[date, dict[str, Decimal]]] = []
    for cube in root.iter():
        if not cube.tag.endswith("Cube") or "time" not in cube.attrib:
            continue
        try:
            effective_date = date.fromisoformat(cube.attrib["time"])
        except ValueError:
            continue
        rates: dict[str, Decimal] = {}
        for child in cube:
            code, rate = child.attrib.get("currency"), child.attrib.get("rate")
            if code is None or rate is None:
                continue
            try:
                rates[_currency(code, "ECB currency")] = Decimal(rate)
            except (InvalidOperation, CurrencyContractError) as exc:
                raise CurrencyContractError("ECB response has an invalid rate") from exc
        if rates and effective_date <= publication_date:
            candidates.append((effective_date, rates))
    if not candidates:
        raise CurrencyContractError("ECB response has no eligible quote")
    effective_date, rates = max(candidates, key=lambda item: item[0])
    quote = FxQuoteReceipt(
        provider="ECB", effective_date=effective_date, fetched_at=fetched_at,
        base_currency="EUR", rates=rates,
        payload_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )
    convert_native_buckets({"EUR": Decimal("0")}, quote, publication_date=publication_date)
    return quote


def fetch_ecb_quote(
    *, publication_date: date, fetched_at: datetime, url: str = DEFAULT_ECB_URL,
    opener: Callable[..., Any] = urlopen,
) -> FxQuoteReceipt:
    request = Request(url, headers={"Accept": "application/xml"})
    try:
        with opener(request, timeout=20) as response:
            payload = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise CurrencyContractError("ECB quote could not be fetched") from exc
    return parse_ecb_quote(payload, publication_date=publication_date, fetched_at=fetched_at)


def _parse_now(value: str | None, zone: ZoneInfo) -> datetime:
    if value is None:
        return datetime.now(zone)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CurrencyContractError("--now must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CurrencyContractError("--now must include a timezone")
    return parsed.astimezone(zone)


def _fetch_ecb_command(
    args: argparse.Namespace, *, opener: Callable[..., Any] = urlopen
) -> int:
    try:
        zone = ZoneInfo(args.publication_date_from_clock)
    except ZoneInfoNotFoundError as exc:
        raise CurrencyContractError("publication clock timezone is invalid") from exc
    now = _parse_now(args.now, zone)
    quote = fetch_ecb_quote(
        publication_date=now.date(), fetched_at=now, url=args.ecb_url, opener=opener
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(quote.to_dict(), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"ECB quote fetched for {quote.effective_date.isoformat()}")
    return 0


def main(
    argv: list[str] | None = None, *, opener: Callable[..., Any] = urlopen
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch-ecb", help="fetch a read-only ECB FX quote receipt")
    fetch.add_argument("--publication-date-from-clock", required=True, metavar="TIMEZONE")
    fetch.add_argument("--output", required=True)
    fetch.add_argument("--ecb-url", default=DEFAULT_ECB_URL, help=argparse.SUPPRESS)
    fetch.add_argument("--now", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return _fetch_ecb_command(args, opener=opener)
    except CurrencyContractError as exc:
        print(f"currency contract error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
