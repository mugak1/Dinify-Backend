"""The published capability levels are a source-authoritative export, not a recollection.

Dinify-Frontend's release gate compares a client's required capability levels
against what the selected backend PUBLISHES. It reads those levels from
``orders_app/contracts/published_capabilities.contract.json`` at an exact commit of
this repository — so the committed file must be what the constants that emit the
wire fields actually say, at every commit, or the gate would be comparing against a
file that has drifted from its source.

These tests assert that UNCONDITIONALLY: no sibling checkout, no environment switch,
nothing that makes them ``skipTest`` in CI (the lesson of the D01 sibling test,
which skipped on every CI run it ever had).
"""
import io
import json
import pathlib
import tempfile
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from orders_app.contracts import published_capabilities
from orders_app.controllers.orders import serializers as order_serializers
from orders_app.controllers.services import checkout_protocol, kitchen_transition, quote_policy, quote_protocol


_COMMITTED = published_capabilities.CONTRACT_FILE


class PublishedCapabilitiesExportTests(SimpleTestCase):
    def test_the_committed_export_matches_the_live_constants(self):
        """THE GATE. A raised level without a regenerated export fails here, alone."""
        self.assertEqual(
            published_capabilities.committed_export(),
            published_capabilities.published_values(),
            'orders_app/contracts/published_capabilities.contract.json is stale. Run '
            '`manage.py export_published_capabilities --write`. A client selects this '
            'backend by commit through a receipt read from that file.',
        )

    def test_the_committed_file_is_byte_for_byte_what_the_exporter_writes(self):
        self.assertEqual(
            published_capabilities.CONTRACT_FILE.read_text(),
            published_capabilities.export_text(),
        )

    def test_each_level_is_the_constant_the_wire_emits(self):
        """Not a copy of the numbers: the same objects the serializers and views use."""
        values = published_capabilities.published_values()
        self.assertIs(values['checkout_protocol'], checkout_protocol.CHECKOUT_PROTOCOL)
        self.assertIs(values['quote_protocol'], quote_protocol.QUOTE_PROTOCOL)
        self.assertIs(values['kitchen_protocol'], kitchen_transition.KITCHEN_PROTOCOL)
        self.assertIs(values['quote_policy_version'], quote_policy.QUOTE_POLICY_VERSION)
        # The order read emits the two diner-facing levels from the same constants.
        self.assertIs(order_serializers.CHECKOUT_PROTOCOL, checkout_protocol.CHECKOUT_PROTOCOL)
        self.assertIs(order_serializers.QUOTE_PROTOCOL, quote_protocol.QUOTE_PROTOCOL)

    def test_every_value_is_a_positive_integer(self):
        for name, value in published_capabilities.published_values().items():
            self.assertIsInstance(value, int, name)
            self.assertNotIsInstance(value, bool, name)
            self.assertGreaterEqual(value, 1, name)

    def test_the_canonical_form_is_the_cross_language_one(self):
        """The frontend digests the same text with release/lib/canonical.mjs."""
        self.assertEqual(
            published_capabilities.canonical_json({'b': 2, 'a': 1, '_note': 'x'}),
            '{"a":1,"b":2}',
        )

    def test_provenance_notes_never_enter_the_digest(self):
        exported = json.loads(published_capabilities.CONTRACT_FILE.read_text())
        self.assertIn('_note', exported)
        self.assertEqual(
            published_capabilities.contract_digest(exported),
            published_capabilities.contract_digest(),
        )


class ExportCommandTests(SimpleTestCase):
    """The command against a TEMPORARY copy, never the committed file.

    A test that rewrites a tracked file and restores it in ``finally`` leaves that
    file corrupted in the working tree whenever the process dies between the two
    writes, and races any other reader of it. ``CONTRACT_FILE`` is read through the
    module at call time, so pointing it at a temporary copy exercises exactly the
    code path the committed file goes through.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.copy = pathlib.Path(directory.name) / 'published_capabilities.contract.json'
        self.copy.write_text(published_capabilities.CONTRACT_FILE.read_text())
        patcher = mock.patch.object(published_capabilities, 'CONTRACT_FILE', self.copy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, *args):
        call_command('export_published_capabilities', *args, stdout=io.StringIO())

    def make_stale(self):
        """Move one published level away from what its constant produces.

        Derived from the CURRENT value, never from a literal (Codex P2 on #331). A
        hardcoded ``"kitchen_protocol": 1`` matches nothing once that level is raised
        and the export regenerated: the "stale" file would then be in step, so the
        check test would fail on a legitimate change and the write test would pass
        having regenerated nothing. The premise is asserted rather than assumed.
        """
        body = json.loads(self.copy.read_text())
        body['kitchen_protocol'] = published_capabilities.published_values()['kitchen_protocol'] + 1
        stale = json.dumps(body, indent=2) + '\n'
        self.assertNotEqual(stale, published_capabilities.export_text(), 'the fixture is not stale')
        self.copy.write_text(stale)

    def test_check_passes_when_in_step(self):
        self.run_command('--check')

    def test_check_fails_loudly_when_the_file_is_stale(self):
        self.make_stale()
        with self.assertRaises(CommandError):
            self.run_command('--check')

    def test_write_regenerates_a_stale_file_to_exactly_the_export(self):
        self.make_stale()
        self.run_command('--write')
        self.assertEqual(self.copy.read_text(), published_capabilities.export_text())
        self.run_command('--check')

    def test_REGRESSION_the_stale_fixture_follows_a_raised_level(self):
        """Codex P2 on #331: raise a level the way a real change would — the constant
        moves and the export is regenerated — and the fixture must still produce a
        file ``--check`` refuses. With the old literal replace, nothing matched after
        the raise, the "stale" copy was in step, and ``--check`` passed."""
        raised = tuple(
            (name, source, value + 1 if name == 'kitchen_protocol' else value)
            for name, source, value in published_capabilities.PUBLISHED
        )
        with mock.patch.object(published_capabilities, 'PUBLISHED', raised):
            self.run_command('--write')
            self.run_command('--check')
            self.make_stale()
            with self.assertRaises(CommandError):
                self.run_command('--check')

    def test_check_and_write_together_are_refused(self):
        with self.assertRaises(CommandError):
            self.run_command('--check', '--write')

    def test_CONTROL_the_committed_file_is_never_the_one_written(self):
        """The patch is what every test above ran against."""
        self.assertNotEqual(published_capabilities.CONTRACT_FILE, _COMMITTED)
