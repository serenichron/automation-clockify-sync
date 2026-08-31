from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_currency as currency


NOW = datetime(2026, 8, 18, 10, 30, tzinfo=timezone.utc)
QUOTE = currency.FxQuoteReceipt(
    provider="ECB",
    effective_date=date(2026, 8, 17),
    fetched_at=NOW,
    base_currency="EUR",
    rates={"USD": Decimal("1.1000"), "RON": Decimal("5.0000")},
    payload_digest="sha256:" + "a" * 64,
)
OLD_QUOTE = currency.FxQuoteReceipt(
    provider="ECB",
    effective_date=date(2026, 8, 13),
    fetched_at=NOW,
    base_currency="EUR",
    rates={"USD": Decimal("1.1000")},
    payload_digest="sha256:" + "b" * 64,
)


class CurrencyContractTests(unittest.TestCase):
    def test_native_buckets_are_preserved_and_summed_as_rounded_usd(self):
        result = currency.convert_native_buckets(
            {"USD": Decimal("983.70"), "EUR": Decimal("7.31")},
            QUOTE,
            publication_date=date(2026, 8, 18),
        )
        self.assertEqual(
            {"USD": Decimal("983.70"), "EUR": Decimal("7.31")},
            result.native_buckets,
        )
        self.assertEqual(Decimal("8.04"), result.usd_buckets["EUR"])
        self.assertEqual(Decimal("991.74"), result.usd_equivalent_total)

    def test_quote_older_than_four_calendar_days_is_rejected(self):
        with self.assertRaisesRegex(currency.CurrencyContractError, "stale"):
            currency.convert_native_buckets(
                {"EUR": Decimal("10")}, OLD_QUOTE, publication_date=date(2026, 8, 18)
            )

    def test_latest_prior_business_day_quote_is_accepted_within_four_days(self):
        friday_quote = currency.FxQuoteReceipt(
            provider="ECB",
            effective_date=date(2026, 8, 14),
            fetched_at=NOW,
            base_currency="EUR",
            rates={"USD": Decimal("1.1000")},
            payload_digest="sha256:" + "c" * 64,
        )
        result = currency.convert_native_buckets(
            {"EUR": Decimal("10")}, friday_quote, publication_date=date(2026, 8, 18)
        )
        self.assertEqual(Decimal("11.00"), result.usd_equivalent_total)

    def test_unknown_currency_missing_rate_and_float_inputs_are_rejected(self):
        for buckets, message in (
            ({"GBP": Decimal("1")}, "rate"),
            ({"EUR": 1.5}, "Decimal"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(currency.CurrencyContractError, message):
                currency.convert_native_buckets(buckets, QUOTE, publication_date=date(2026, 8, 18))
        with self.assertRaisesRegex(currency.CurrencyContractError, "USD"):
            currency.FxQuoteReceipt(
                provider="ECB", effective_date=date(2026, 8, 17), fetched_at=NOW,
                base_currency="EUR", rates={"RON": Decimal("5")},
                payload_digest="sha256:" + "d" * 64,
            )

    def test_cross_rate_uses_half_up_cent_rounding(self):
        result = currency.convert_native_buckets(
            {"RON": Decimal("1.00")}, QUOTE, publication_date=date(2026, 8, 18)
        )
        self.assertEqual(Decimal("0.22"), result.usd_buckets["RON"])
        self.assertEqual(Decimal("0.22"), result.usd_equivalent_total)

    def test_serialized_summary_with_arithmetic_drift_is_rejected(self):
        summary = currency.convert_native_buckets(
            {"EUR": Decimal("10")}, QUOTE, publication_date=date(2026, 8, 18)
        ).to_dict()
        summary["usd_equivalent_total"] = "10.99"
        with self.assertRaisesRegex(currency.CurrencyContractError, "total"):
            currency.CurrencySummary.from_dict(summary)

    def test_quote_rejects_invalid_provider_base_rate_and_digest(self):
        invalid = {
            "provider": "other", "base_currency": "USD",
            "rates": {"USD": Decimal("0")}, "payload_digest": "digest",
        }
        for field, value in invalid.items():
            fields = {
                "provider": "ECB", "effective_date": date(2026, 8, 17), "fetched_at": NOW,
                "base_currency": "EUR", "rates": {"USD": Decimal("1.1")},
                "payload_digest": "sha256:" + "e" * 64,
            }
            fields[field] = value
            with self.subTest(field=field), self.assertRaises(currency.CurrencyContractError):
                currency.FxQuoteReceipt(**fields)


class FetchEcbCliTests(unittest.TestCase):
    def test_fetch_ecb_selects_latest_eligible_quote_and_writes_a_receipt(self):
        payload = b'''<?xml version="1.0"?>
        <gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
          <Cube><Cube time="2026-08-17"><Cube currency="USD" rate="1.1000"/><Cube currency="RON" rate="5.0000"/></Cube><Cube time="2026-08-19"><Cube currency="USD" rate="1.2000"/></Cube></Cube>
        </gesmes:Envelope>'''
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return payload

        def opener(request, *, timeout):
            calls.append((request.get_method(), request.full_url, timeout))
            return Response()

        quote = currency.fetch_ecb_quote(
            publication_date=date(2026, 8, 18), fetched_at=NOW,
            url="https://ecb.fixture.test/eurofxref.xml", opener=opener,
        )
        self.assertEqual([( "GET", "https://ecb.fixture.test/eurofxref.xml", 20)], calls)
        self.assertEqual(date(2026, 8, 17), quote.effective_date)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ecb.xml"
            output = Path(directory) / "fx-receipt.json"
            source.write_bytes(payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                status = currency.main(
                [
                    "fetch-ecb",
                    "--publication-date-from-clock", "Europe/Bucharest",
                    "--now", "2026-08-18T10:30:00+00:00",
                    "--output", str(output), "--ecb-url", source.as_uri(),
                ]
                )
            self.assertEqual(0, status)
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("fx-quote-receipt/v1", receipt["schema_version"])
        self.assertEqual("ECB", receipt["provider"])
        self.assertEqual("EUR", receipt["base_currency"])
        self.assertEqual("2026-08-17", receipt["effective_date"])
        self.assertEqual("1.1000", receipt["rates"]["USD"])
        self.assertEqual("sha256:" + hashlib.sha256(payload).hexdigest(), receipt["payload_digest"])
        self.assertNotIn(payload.decode(), stdout.getvalue())
        self.assertNotIn("credential", stdout.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
