"""
The admission lock primitive's own guard — it must refuse to run in autocommit.

``pg_advisory_xact_lock`` is released by the transaction that took it. Taken in
autocommit there IS no transaction, so the lock is released by the statement that
acquired it and the caller holds nothing — silently. Every test would still pass; the
protection would simply not exist. That failure mode is the reason the guard sits in
the primitive rather than only in ``order_admission.admit()``, which is one of two
call sites and cannot speak for the other.

``TransactionTestCase``, not ``TestCase``: the latter wraps every test in a
transaction, which is precisely the condition under test and would make the guard
untestable.

The behaviour is backend-independent — the check runs before the PostgreSQL vendor
branch, on purpose — so unlike the concurrency suites this one does NOT skip on
SQLite. That is exactly the run where a future caller would otherwise get no signal.
"""
import uuid

from django.db import transaction
from django.test import TransactionTestCase

from restaurants_app.controllers.admission_lock import (
    _lock, advisory_key, lock_admission_exclusive, lock_admission_shared,
)


class AdmissionLockTransactionGuardTests(TransactionTestCase):
    reset_sequences = False

    def setUp(self):
        super().setUp()
        # The primitive folds a restaurant id into an advisory key; it never reads
        # the row, so no fixture is needed to exercise it.
        self.restaurant_id = uuid.uuid4()

    def test_lock_refuses_autocommit(self):
        """The primitive itself raises, not just the caller that remembered to check."""
        with self.assertRaises(RuntimeError) as caught:
            _lock(self.restaurant_id, exclusive=True)

        self.assertIn('inside a transaction', str(caught.exception))

    def test_both_public_wrappers_refuse_autocommit(self):
        for take in (lock_admission_shared, lock_admission_exclusive):
            with self.subTest(take=take.__name__):
                with self.assertRaises(RuntimeError):
                    take(self.restaurant_id)

    def test_inside_a_transaction_it_is_allowed(self):
        """The guard is about autocommit only — a real caller is unaffected."""
        with transaction.atomic():
            lock_admission_shared(self.restaurant_id)
            lock_admission_exclusive(self.restaurant_id)

    def test_advisory_key_is_deterministic_and_accepts_a_string(self):
        self.assertEqual(
            advisory_key(self.restaurant_id), advisory_key(str(self.restaurant_id)),
        )
