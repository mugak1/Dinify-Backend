"""
A1(C) — THE CLOSURE SERVICE'S STATED PRECONDITIONS, VERIFIED.

`quote_closure.close` writes the one irreversible fact in D06: a saved quote
that may never be accepted. Its docstring lists what must hold before it will
do that, and a precondition nobody exercises is a comment rather than a rule.
This suite fires each of them.

IT ALSO CORRECTS AN OVERCLAIM RATHER THAN PAPERING OVER IT. The docstring used
to say the whole "inside the caller's transaction, holding the locks an
acceptance takes" sentence was "asserted rather than assumed". The transaction
half is asserted and raises; the LOCK half is not, and cannot be — PostgreSQL
keeps a row lock on the tuple, not in `pg_locks`, so no query answers "do I
hold FOR UPDATE on this row", and a `FOR UPDATE NOWAIT` probe succeeds exactly
when nobody holds it. So the guarantee is structural instead, and the scan at
the end of this file is what keeps it true.

WHAT IS DELIBERATELY NOT DONE HERE, per the mandate: no caller-supplied
"already locked" flag (a trusted switch would let the one caller that gets it
wrong assert its way past the only barrier there is), no broad exception
suppression, and nothing that repairs or rewrites an existing row.
"""
import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from orders_app.controllers.services import quote_closure
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderAcceptance, OrderQuoteClosure
from orders_app.tests_quote_lifetime import QuoteFixtureMixin

REPO = pathlib.Path(__file__).resolve().parent.parent


class ClosurePreconditionsReallyFireTests(QuoteFixtureMixin, TestCase):

    def _a_draft(self):
        return self._draft_order()

    # -- 1. the transaction --------------------------------------------------

    def test_it_refuses_to_run_outside_a_transaction(self):
        """The one lock-adjacent precondition that IS assertable. A closure
        written in autocommit beside an acceptance still deciding is the
        double-purchase this service exists to prevent, and it fails silently.

        `TestCase` wraps every test in a transaction, so autocommit is modelled
        by patching the one thing `close` actually consults — which is also the
        honest way to state what it checks and what it cannot.
        """
        order = self._a_draft()
        with patch.object(
            quote_closure.transaction, 'get_connection',
            return_value=SimpleNamespace(in_atomic_block=False),
        ):
            with self.assertRaises(RuntimeError) as raised:
                quote_closure.close(
                    order, quote_ref=quote_ref(order),
                    reason=quote_closure.REASON_EXPIRED,
                    now=timezone.now(), evidence=None,
                )
        self.assertIn('inside the caller', str(raised.exception))
        self.assertFalse(OrderQuoteClosure.objects.exists())

    # -- 2. the reason vocabulary -------------------------------------------

    def test_a_reason_outside_the_vocabulary_raises_LOUDLY(self):
        """Programmer error, not a caller outcome. A TRANSIENT refusal reaching
        here would permanently destroy a perfectly good quote, so it must not be
        coerced into the nearest valid reason."""
        order = self._a_draft()
        for reason in ('restaurant_paused', 'quote_ref_stale', '', None):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    quote_closure.close(
                        order, quote_ref=quote_ref(order), reason=reason,
                        now=timezone.now(), evidence=None,
                    )
        self.assertFalse(OrderQuoteClosure.objects.exists())

    def test_the_vocabulary_is_exactly_the_two_the_database_allows(self):
        from orders_app.models import QUOTE_CLOSURE_REASONS
        self.assertEqual(
            set(QUOTE_CLOSURE_REASONS),
            {quote_closure.REASON_EXPIRED,
             quote_closure.REASON_PURCHASE_CHANGED},
        )

    # -- 3. the reference ----------------------------------------------------

    def test_a_missing_or_non_string_reference_is_a_controlled_refusal(self):
        """A closure retires ONE named reference. A caller that cannot name the
        quote it means must not retire whatever happens to be current."""
        order = self._a_draft()
        for ref in (None, '', 123, b'x'):
            with self.subTest(ref=ref):
                with self.assertRaises(quote_closure.ClosureRefused) as raised:
                    quote_closure.close(
                        order, quote_ref=ref,
                        reason=quote_closure.REASON_EXPIRED,
                        now=timezone.now(), evidence=None,
                    )
                self.assertEqual(
                    raised.exception.reason,
                    quote_closure.REASON_QUOTE_REF_MISMATCH)
        self.assertFalse(OrderQuoteClosure.objects.exists())

    # -- 4. acceptance is resolved FIRST ------------------------------------

    def test_an_ACCEPTED_order_can_never_be_closed(self):
        order = self._a_draft()
        OrderAcceptance.objects.create(
            order=order, quote_ref=quote_ref(order), accepted_at=timezone.now())

        with self.assertRaises(quote_closure.ClosureRefused) as raised:
            quote_closure.close(
                order, quote_ref=quote_ref(order),
                reason=quote_closure.REASON_EXPIRED,
                now=timezone.now(), evidence=None,
            )
        self.assertEqual(raised.exception.reason,
                         quote_closure.REASON_ALREADY_ACCEPTED)
        self.assertFalse(OrderQuoteClosure.objects.exists())

    def test_a_NON_DRAFT_with_no_evidence_stays_D04s_statement_of_ignorance(self):
        """It must not be converted into a terminal fact. The server does not
        know whether that submission landed, and a closure would assert it did
        not."""
        order = self._a_draft()
        Order.objects.filter(pk=order.pk).update(order_status='pending')
        order.refresh_from_db()

        with self.assertRaises(quote_closure.ClosureRefused) as raised:
            quote_closure.close(
                order, quote_ref=quote_ref(order),
                reason=quote_closure.REASON_EXPIRED,
                now=timezone.now(), evidence=None,
            )
        self.assertEqual(raised.exception.reason,
                         quote_closure.REASON_EVIDENCE_UNAVAILABLE)
        self.assertFalse(OrderQuoteClosure.objects.exists())

    # -- 5. re-closing returns the ORIGINAL, unchanged -----------------------

    def test_re_closing_returns_the_original_row_and_rewrites_nothing(self):
        order = self._a_draft()
        first = quote_closure.close(
            order, quote_ref=quote_ref(order),
            reason=quote_closure.REASON_EXPIRED,
            now=timezone.now(), evidence=None,
        ).closure

        again = quote_closure.close(
            order, quote_ref=quote_ref(order),
            reason=quote_closure.REASON_PURCHASE_CHANGED,
            now=timezone.now(), evidence=None,
        )
        self.assertEqual(again.outcome, quote_closure.OUTCOME_ALREADY_CLOSED)
        again.closure.refresh_from_db()
        self.assertEqual(again.closure.pk, first.pk)
        self.assertEqual(again.closure.reason, quote_closure.REASON_EXPIRED)
        self.assertEqual(again.closure.closed_at, first.closed_at)

    def test_a_DIFFERENT_reference_never_rewrites_a_committed_closure(self):
        order = self._a_draft()
        quote_closure.close(
            order, quote_ref=quote_ref(order),
            reason=quote_closure.REASON_EXPIRED,
            now=timezone.now(), evidence=None,
        )
        with self.assertRaises(quote_closure.ClosureRefused) as raised:
            quote_closure.close(
                order, quote_ref='some-other-reference',
                reason=quote_closure.REASON_EXPIRED,
                now=timezone.now(), evidence=None,
            )
        self.assertEqual(raised.exception.reason,
                         quote_closure.REASON_QUOTE_REF_MISMATCH)
        self.assertEqual(OrderQuoteClosure.objects.count(), 1)


class EveryClosurePathLocksTheOrderFirstTests(TestCase):
    """THE STRUCTURAL HALF, since the lock cannot be asserted at the callee.

    The rule: any production function that can reach `quote_closure.close` must
    take `Order.objects.select_for_update()` before it does. The scan is
    deliberately narrow — it reads the source, names the functions it found, and
    fails on one that does not lock — rather than trying to prove reachability in
    general.
    """

    #: The production entry points that reach `close`, as an INVENTORY rather
    #: than a count: a new one is a deliberate edit to this list, with the lock
    #: it must take checked below.
    #: `_retire` is a closure defined INSIDE `_terminal_quote_outcome`, so an
    #: `ast.walk` over the module legitimately reports both. Naming both is more
    #: honest than filtering to the innermost: what a reader needs is the set of
    #: source functions that can reach `close`.
    EXPECTED = {'_retire', '_terminal_quote_outcome'}

    def _module(self):
        path = REPO / 'orders_app' / 'controllers' / 'manage_order.py'
        return path, ast.parse(path.read_text())

    def _functions_calling_close(self, tree):
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if (isinstance(func, ast.Attribute) and func.attr == 'close'
                        and isinstance(func.value, ast.Name)
                        and func.value.id == 'quote_closure'):
                    found[node.name] = node
        return found

    def test_the_inventory_is_exactly_what_the_source_holds(self):
        _, tree = self._module()
        self.assertEqual(set(self._functions_calling_close(tree)),
                         self.EXPECTED)

    def test_every_enclosing_boundary_locks_the_order_row(self):
        """`_retire` is a closure defined inside `_terminal_quote_outcome`, which
        is itself called only from the two locked boundaries. So the assertion is
        made about THOSE — the functions that actually hold a transaction."""
        path, tree = self._module()
        source = path.read_text()

        callers = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == '_terminal_quote_outcome'
                for call in ast.walk(node)
            )
        ]
        self.assertTrue(callers, 'the scan found no boundary to check')
        for node in callers:
            with self.subTest(function=node.name):
                body = ast.get_source_segment(source, node) or ''
                self.assertIn(
                    'Order.objects.select_for_update()', body,
                    f'{node.name} reaches quote_closure.close without taking '
                    'the order row lock first',
                )

    def test_there_is_no_caller_supplied_trusted_or_already_locked_flag(self):
        """A trusted switch would let the one caller that gets it wrong assert
        its way past the only barrier there is."""
        source = (REPO / 'orders_app' / 'controllers' / 'services'
                  / 'quote_closure.py').read_text()
        for banned in ('trusted', 'already_locked', 'skip_lock', 'force'):
            self.assertNotIn(f'{banned}=', source)
            self.assertNotIn(f'{banned} =', source)

    def test_close_suppresses_no_exceptions_broadly(self):
        source = (REPO / 'orders_app' / 'controllers' / 'services'
                  / 'quote_closure.py').read_text()
        tree = ast.parse(source)
        close = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == 'close')
        for handler in [n for n in ast.walk(close)
                        if isinstance(n, ast.ExceptHandler)]:
            self.fail(
                'close() must not catch anything: its refusals are raised, and '
                'a swallowed error here writes or withholds an irreversible '
                f'fact silently (found handler at line {handler.lineno})')
