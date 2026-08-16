from __future__ import annotations

import io
import json
import unittest
from unittest import mock
import urllib.error

from scripts import clockify_post_approved_portfolio as poster
from scripts import clockify_sync_collect as collector


class _Response:
    def __init__(self, value: object):
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class ClockifyCollectorTimeoutTests(unittest.TestCase):
    def test_configured_timeout_reaches_clockify_http_boundary(self):
        environment = {
            "CLOCKIFY_API_KEY": "fixture-key",
            "CLOCKIFY_HTTP_TIMEOUT_SECONDS": "67",
        }
        with mock.patch.dict(collector.os.environ, {}, clear=True), mock.patch.object(
            collector.urllib.request,
            "urlopen",
            return_value=_Response([]),
        ) as urlopen:
            self.assertEqual([], collector.clockify_get("/fixture", environment))

        self.assertEqual(67, urlopen.call_args.kwargs["timeout"])

    def test_missing_timeout_preserves_thirty_second_default(self):
        environment = {"CLOCKIFY_API_KEY": "fixture-key"}
        with mock.patch.dict(collector.os.environ, {}, clear=True), mock.patch.object(
            collector.urllib.request,
            "urlopen",
            return_value=_Response([]),
        ) as urlopen:
            collector.clockify_get("/fixture", environment)

        self.assertEqual(30, urlopen.call_args.kwargs["timeout"])

    def test_invalid_timeout_fails_before_clockify_http_boundary(self):
        for value in ("", "abc", "4", "121", "１２"):
            environment = {
                "CLOCKIFY_API_KEY": "fixture-key",
                "CLOCKIFY_HTTP_TIMEOUT_SECONDS": value,
            }
            with self.subTest(value=value), mock.patch.dict(
                collector.os.environ, {}, clear=True
            ), mock.patch.object(collector.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(ValueError):
                    collector.clockify_get("/fixture", environment)
                urlopen.assert_not_called()


class ClockifyPosterTimeoutTests(unittest.TestCase):
    def test_configured_timeout_reaches_posting_http_boundary(self):
        with mock.patch.object(
            poster.urllib.request,
            "urlopen",
            return_value=_Response({"id": "entry-one"}),
        ) as urlopen:
            result = poster._request(
                "/fixture",
                "fixture-key",
                method="POST",
                payload={"description": "fixture"},
                timeout_seconds=83,
            )

        self.assertEqual({"id": "entry-one"}, result)
        self.assertEqual(83, urlopen.call_args.kwargs["timeout"])

    def test_timeout_parser_uses_default_and_configured_values(self):
        with mock.patch.dict(poster.os.environ, {}, clear=True):
            self.assertEqual(45, poster.post_http_timeout_seconds({}))
            self.assertEqual(
                83,
                poster.post_http_timeout_seconds(
                    {"CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS": "83"}
                ),
            )

    def test_invalid_timeout_fails_before_posting_http_boundary(self):
        for value in ("", "abc", "4", "121", "１２"):
            with self.subTest(value=value), mock.patch.dict(
                poster.os.environ, {}, clear=True
            ), mock.patch.object(poster.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(poster.PortfolioPostError):
                    poster.post_http_timeout_seconds(
                        {"CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS": value}
                    )
                urlopen.assert_not_called()

    def test_get_retry_count_is_unchanged_with_configured_timeout(self):
        with mock.patch.object(
            poster.urllib.request,
            "urlopen",
            side_effect=TimeoutError("fixture timeout"),
        ) as urlopen, mock.patch.object(poster.time, "sleep"):
            with self.assertRaises(poster.PortfolioPostError):
                poster._request("/fixture", "fixture-key", timeout_seconds=83)

        self.assertEqual(4, urlopen.call_count)
        self.assertEqual(
            [83, 83, 83, 83],
            [call.kwargs["timeout"] for call in urlopen.call_args_list],
        )

    def test_ambiguous_post_server_error_is_not_blindly_retried(self):
        failure = urllib.error.HTTPError(
            "https://api.clockify.me/fixture",
            500,
            "server error",
            {},
            io.BytesIO(),
        )
        with mock.patch.object(
            poster.urllib.request,
            "urlopen",
            side_effect=failure,
        ) as urlopen, mock.patch.object(poster.time, "sleep") as sleep:
            with self.assertRaises(poster.PortfolioPostError):
                poster._request(
                    "/fixture",
                    "fixture-key",
                    method="POST",
                    payload={"description": "fixture"},
                    timeout_seconds=83,
                )

        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()
        self.assertTrue(failure.closed)


if __name__ == "__main__":
    unittest.main()
