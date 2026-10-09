"""Comparison-only sealed event correspondence; never canonical timing atoms.

The sole lossy compatibility is a historical Bucharest minute timestamp
refining to an aware precise instant in that same local minute. All source
locator and content identity remains exact. Correspondence says nothing about
whether two activities accomplished the same work.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from zoneinfo import ZoneInfo


def _identity(event):
    ref, attrs = event.get('source_ref', {}), event.get('attributes', {})
    values = (event.get('source_type'), ref.get('source_type'), ref.get('machine'),
              ref.get('session_id'), ref.get('source_id'), attrs.get('role'), attrs.get('kind'))
    content, ordinal = attrs.get('content'), ref.get('ordinal')
    if (any(not isinstance(value, str) or not value.strip() for value in values)
            or attrs.get('role') not in {'user', 'assistant'}
            or type(ordinal) is not int or ordinal < 0
            or not isinstance(content, str) or not content):
        return None
    return (*values, ordinal, hashlib.sha256(content.encode('utf-8')).hexdigest())


def _timestamp(value, historical_timezone):
    if not isinstance(value, str):
        return None
    legacy = bool(re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', value))
    try:
        if legacy:
            if historical_timezone != 'Europe/Bucharest':
                return None
            local = datetime.strptime(value, '%Y-%m-%d %H:%M')
            zone = ZoneInfo('Europe/Bucharest')
            # Attaching ZoneInfo silently chooses a fold and also accepts gap
            # times. A lossy minute has no fold authority: it must roundtrip
            # to exactly one real UTC instant before any equality/refinement.
            instants = set()
            for fold in (0, 1):
                candidate = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                if candidate.astimezone(zone).replace(tzinfo=None) == local:
                    instants.add(candidate)
            if len(instants) != 1:
                return None
            instant = next(iter(instants)).astimezone(zone)
        else:
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if instant.tzinfo is None or instant.utcoffset() is None:
                return None
        return instant, legacy
    except (ValueError, TypeError):
        return None


def corresponds(first, second, *, historical_timezone=None):
    """Require exact sealed identity and an authenticated timestamp relation."""
    identity = _identity(first)
    if identity is None or identity != _identity(second):
        return False
    left = _timestamp(first.get('observed_at'), historical_timezone)
    right = _timestamp(second.get('observed_at'), historical_timezone)
    if left is None or right is None:
        return False
    a, legacy_a = left
    b, legacy_b = right
    if a.astimezone(timezone.utc) == b.astimezone(timezone.utc):
        return True
    if legacy_a == legacy_b:
        return False
    zone = ZoneInfo('Europe/Bucharest')
    return a.astimezone(zone).replace(second=0, microsecond=0) == b.astimezone(zone).replace(second=0, microsecond=0)


def pairs(selected_events, counterpart_events, *, historical_timezone):
    """Unique one-to-one event matches only; ambiguous locators grant no coverage."""
    result, used = [], set()
    for selected in selected_events:
        matches = [index for index, other in enumerate(counterpart_events)
                   if corresponds(selected, other, historical_timezone=historical_timezone)]
        if len(matches) == 1 and matches[0] not in used:
            index = matches[0]
            used.add(index)
            result.append((selected['evidence_id'], counterpart_events[index]['evidence_id']))
    return result
