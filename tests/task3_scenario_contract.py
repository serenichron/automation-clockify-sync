"""Shared assertions for each Task 3 end-to-end recovery scenario."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import unittest


def assert_scenario_contract(
    case: unittest.TestCase,
    *,
    stable_ids: Iterable[str],
    parent_before: Mapping[str, bytes],
    parent_after: Mapping[str, bytes],
    emitted_ids: Iterable[str],
    clockify_adapter_calls: int,
) -> None:
    stable = list(stable_ids)
    emitted = list(emitted_ids)
    case.assertTrue(stable, "scenario must expose at least one stable identity")
    case.assertEqual(len(stable), len(set(stable)), "stable identities must be unique")
    case.assertTrue(parent_before, "scenario must snapshot immutable parent bytes")
    case.assertEqual(dict(parent_before), dict(parent_after))
    case.assertEqual(len(emitted), len(set(emitted)), "rows/receipts/events must be duplicate-free")
    case.assertEqual(0, clockify_adapter_calls, "Clockify write adapter calls must remain zero")
