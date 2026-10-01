import unittest

from scripts import work_accounting_pipeline as pipeline


class ScopedTurnOrderTests(unittest.TestCase):
    def test_source_order_keeps_each_instruction_with_its_reply_despite_clock_skew(self):
        events = []
        for ordinal, role, timestamp in (
            (1, 'user', '2026-09-25T10:00:00+03:00'),
            (2, 'assistant', '2026-09-25T09:58:00+03:00'),
            (3, 'user', '2026-09-25T09:59:00+03:00'),
            (4, 'assistant', '2026-09-25T10:01:00+03:00'),
        ):
            events.append({
                'evidence_id': f'ev-{ordinal}', 'source_type': 'claude_bursts_event',
                'observed_at': timestamp,
                'source_ref': {'session_id': 'session-one', 'machine': 'fixture',
                               'source_type': 'claude_bursts_event', 'ordinal': ordinal},
                'attributes': {'kind': 'message', 'role': role, 'content': 'work'},
            })
        partitions = pipeline._scoped_review_partitions(events, maximum_members=2)
        self.assertEqual([['ev-1', 'ev-2'], ['ev-3', 'ev-4']],
                         [[row['evidence_id'] for row in part] for part in partitions])
