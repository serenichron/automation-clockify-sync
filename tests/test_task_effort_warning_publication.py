import copy
import unittest

from scripts import clockify_sheet_publish as publisher


class TaskEffortWarningPublicationTests(unittest.TestCase):
    def warning(self):
        return {
            'type': 'estimated_task_effort',
            'timing_recipe': 'task_owned_message_observations/v1',
            'reason': 'Estimated commissioned task effort bounded by task-owned source observations; not human attention or measured agent runtime.',
            'observed_intervals': [
                {'start': '2026-10-19T09:00:00+03:00', 'end': '2026-10-19T09:05:00+03:00'},
            ],
        }

    def test_task_estimate_is_visible_without_publishing_private_observation_windows(self):
        # A publisher rejecting the new accounting warning would block every
        # positive v9 proposal; stripping it would hide the changed time basis.
        source = self.warning()
        before = copy.deepcopy(source)
        try:
            rendered = publisher._validate_review_warning(source, {})
        except publisher.PublicationError as error:
            self.fail(f'Valid task-effort warning must reach review: {error}')
        self.assertEqual({
            'type': 'estimated_task_effort',
            'timing_recipe': 'task_owned_message_observations/v1',
            'reason': 'Estimated commissioned task effort bounded by task-owned source observations; not human attention or measured agent runtime.',
        }, rendered)
        self.assertEqual(before, source)

    def test_unknown_fields_or_unproved_timing_are_not_publishable(self):
        cases = [
            dict(self.warning(), raw_evidence='private text'),
            dict(self.warning(), timing_recipe='human_attendance'),
            dict(self.warning(), reason='measured human time'),
            dict(self.warning(), observed_intervals=[]),
            dict(self.warning(), observed_intervals=[{'start': '2026-10-19T09:05:00+03:00', 'end': '2026-10-19T09:00:00+03:00'}]),
            dict(self.warning(), observed_intervals=[{'start': '2026-10-19T09:00:00', 'end': '2026-10-19T09:05:00'}]),
        ]
        for warning in cases:
            with self.subTest(warning=warning), self.assertRaises(publisher.PublicationError):
                publisher._validate_review_warning(warning, {})

    def test_subsecond_private_observations_do_not_block_review_warning(self):
        warning = self.warning()
        warning['observed_intervals'] = [
            {'start': '2026-10-19T09:00:00.123456+03:00',
             'end': '2026-10-19T09:05:00.987654+03:00'},
        ]
        try:
            rendered = publisher._validate_review_warning(warning, {})
        except publisher.PublicationError as error:
            self.fail(f'Genuine subsecond observation bounds must remain valid: {error}')
        self.assertEqual('estimated_task_effort', rendered['type'])
        self.assertNotIn('observed_intervals', rendered)


if __name__ == '__main__':
    unittest.main()
