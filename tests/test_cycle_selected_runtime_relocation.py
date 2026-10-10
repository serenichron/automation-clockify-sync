"""Consumer-boundary proof: relocation is allowed only for identical code bytes."""
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import clockify_selected_delivery_adoption as selected
from scripts import clockify_mixed_review_availability as mixed
import test_cycle_selected_delivery_adoption as fixtures


class SelectedRuntimeRelocationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SelectedHistoricalDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.old_runtime = self.fixture.root/'old-release'/'clockify_mixed_review_availability.py'
        self.old_runtime.parent.mkdir()
        self.old_runtime.write_bytes(Path(mixed.__file__).read_bytes())
        self.original_validate = selected.validate
        self.runtime = self.old_runtime
        # The real native selected proof still runs. Only the mixed consumer's
        # output runtime handle is supplied at the verifier boundary, avoiding
        # unrelated financial/own-accomplishment fixture construction.
        def validate(*args, **kwargs):
            document = self.original_validate(*args, **kwargs)
            return {**document, 'consumer_runtime': {
                'path': str(self.runtime), 'sha256': cycle._digest(self.runtime)}}
        self.patch = mock.patch.object(selected, 'validate', side_effect=validate)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.fixture.adopt()
        self.state_path = self.fixture.state_dir/'review-cycle-state.json'
        self.state = json.loads(self.state_path.read_text())
        record = self.state['slices'][self.fixture.since]
        self.paths = [self.state_path, Path(record['delivery_receipt']), Path(record['historical_adoption_receipt'])]
        self.before = {path: path.read_bytes() for path in self.paths}
        self.runtime = Path(mixed.__file__).resolve()

    def verify_delivery(self):
        record = self.state['slices'][self.fixture.since]
        cycle._verify_delivery_receipt(Path(record['delivery_receipt']), self.fixture.config,
            self.fixture.since, self.fixture.until, record['source'], record['replay'],
            sheet_title=self.fixture.title)

    def test_identical_runtime_bytes_at_new_path_verify_without_rewriting_receipts(self):
        """Exact path equality must not reject an authenticated byte-identical relocation."""
        try:
            self.verify_delivery()
            cycle._validate_delivered_state(self.fixture.config, self.state)
            result = self.fixture.adopt()
        except cycle.CycleError as error:
            self.fail(f'Byte-identical authenticated runtime relocation must verify: {error}')
        self.assertEqual('delivered_with_exceptions', result['status'])
        self.assertEqual(self.before, {path: path.read_bytes() for path in self.paths})

    def test_changed_historical_runtime_bytes_are_rejected(self):
        """Trusting just the saved digest would accept a drifted original code file."""
        self.old_runtime.write_bytes(b'changed historical code')
        with self.assertRaises(cycle.CycleError):
            self.verify_delivery()
        with self.assertRaises(cycle.CycleError):
            self.fixture.adopt()

    def test_absent_historical_runtime_is_rejected(self):
        """A current digest match cannot substitute for absent original runtime proof."""
        self.old_runtime.unlink()
        with self.assertRaises(cycle.CycleError):
            self.verify_delivery()
        with self.assertRaises(cycle.CycleError):
            self.fixture.adopt()

    def test_different_current_runtime_bytes_are_rejected(self):
        """Relocation never authorizes a different runtime digest."""
        self.runtime = self.fixture.root/'changed-runtime.py'
        self.runtime.write_bytes(b'different code')
        with self.assertRaises(cycle.CycleError):
            self.verify_delivery()
        with self.assertRaises(cycle.CycleError):
            self.fixture.adopt()

    def test_semantic_or_extra_delivery_field_drift_is_rejected(self):
        """The relocation exception must leave every other output field exact."""
        for update in ({'adoption_provider_writes': 1}, {'unrecognized': True}):
            with self.subTest(update=update):
                def changed(*args, **kwargs):
                    value = self.original_validate(*args, **kwargs)
                    return {**value, 'consumer_runtime': {
                        'path': str(self.runtime), 'sha256': cycle._digest(self.runtime)}, **update}
                with mock.patch.object(selected, 'validate', side_effect=changed):
                    with self.assertRaises(cycle.CycleError):
                        self.verify_delivery()
                    with self.assertRaises(cycle.CycleError):
                        self.fixture.adopt()
