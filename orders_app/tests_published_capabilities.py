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
import json

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from orders_app.contracts import published_capabilities
from orders_app.controllers.orders import serializers as order_serializers
from orders_app.controllers.services import checkout_protocol, kitchen_transition, quote_policy, quote_protocol


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
    def test_check_passes_when_in_step(self):
        call_command('export_published_capabilities', '--check', stdout=open('/dev/null', 'w'))

    def test_check_fails_loudly_when_the_file_is_stale(self):
        original = published_capabilities.CONTRACT_FILE.read_text()
        try:
            published_capabilities.CONTRACT_FILE.write_text(original.replace('"kitchen_protocol": 1', '"kitchen_protocol": 9'))
            with self.assertRaises(CommandError):
                call_command('export_published_capabilities', '--check', stdout=open('/dev/null', 'w'))
        finally:
            published_capabilities.CONTRACT_FILE.write_text(original)

    def test_check_and_write_together_are_refused(self):
        with self.assertRaises(CommandError):
            call_command('export_published_capabilities', '--check', '--write', stdout=open('/dev/null', 'w'))
