"""
Delete OTP accounting rows that are past retention (D11 B2-C).

A row is ELIGIBLE once it is strictly older than seven days, in any state. This command
removes eligible rows in bounded, oldest-first batches from both ledger tables and
prints COUNTS ONLY — never a key, an id, a timestamp or database text. The retention is
fixed in code and cannot be overridden here, and nothing schedules this command: it
runs when an operator runs it.

    python manage.py prune_otp_accounting [--batch-size N] [--max-batches M]

WHAT IT REPORTS, AND WHAT IT MEANS.

* One line per batch, then a total per table. Every count printed is a delete that has
  COMMITTED: the command requires autocommit (it refuses to run inside a caller's
  transaction, including one opened by turning autocommit off), and each table's delete
  commits before the next statement starts.
* ``complete: no eligible rows found when last checked`` only after a batch in which
  every table came back short AND a fresh check then found nothing eligible. A short
  batch alone is not enough: a concurrent cleanup can take rows this batch selected
  while other eligible rows remain. The claim is about that moment only — rows keep
  crossing the retention boundary as time passes.
* ``batch limit reached: eligible rows may remain`` when ``--max-batches`` ran out.
* ON FAILURE it exits non-zero (1). Everything confirmed so far has already been
  printed; the failed statement's outcome is called UNKNOWN (it may have been refused,
  or have committed just before the connection was lost); and the message carries a
  fixed category, never the database's text. Nothing is retried.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from users_app import otp_accounting

DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 100
MAX_BATCHES = 10000

TABLES = otp_accounting.PRUNE_TABLE_NAMES


class Command(BaseCommand):
    help = 'Delete OTP accounting rows strictly older than the fixed seven-day retention.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=DEFAULT_BATCH_SIZE,
            help=f'Rows per table per batch (1-{otp_accounting.MAX_PRUNE_BATCH}).',
        )
        parser.add_argument(
            '--max-batches', type=int, default=DEFAULT_MAX_BATCHES,
            help=f'Stop after this many batches (1-{MAX_BATCHES}).',
        )

    def handle(self, *args, batch_size, max_batches, **options):
        if not 1 <= batch_size <= otp_accounting.MAX_PRUNE_BATCH:
            raise CommandError(
                f'--batch-size must be between 1 and {otp_accounting.MAX_PRUNE_BATCH}.'
            )
        if not 1 <= max_batches <= MAX_BATCHES:
            raise CommandError(f'--max-batches must be between 1 and {MAX_BATCHES}.')
        if connection.in_atomic_block or not connection.get_autocommit():
            # Inside a caller's transaction — an atomic block, or autocommit turned off —
            # nothing below would be committed when it is reported, the caller could
            # still roll it back, and every batch would share one transaction.
            raise CommandError('refusing to run inside a transaction; nothing was deleted.')

        totals = dict.fromkeys(TABLES, 0)
        for batch in range(1, max_batches + 1):
            try:
                counts = otp_accounting.prune(batch_size=batch_size)
            except otp_accounting.PruneFailure as failure:
                self._add(totals, failure.completed)
                self._report_incomplete_batch(batch, failure)
                self._fail(
                    totals, failure.category,
                    f'the {failure.failed_table} delete in batch {batch} failed and its '
                    'outcome is unknown',
                )
            except Exception as exc:  # noqa: BLE001 - reported by category only
                self._fail(
                    totals, otp_accounting.failure_category(exc),
                    f'batch {batch} failed before any delete was confirmed and its '
                    'outcome is unknown',
                )
            self._add(totals, counts)
            self.stdout.write(
                f'batch {batch}: '
                + ', '.join(f'{table} deleted {counts[table]}' for table in TABLES)
            )
            if any(counts[table] >= batch_size for table in TABLES):
                continue
            try:
                remaining = otp_accounting.eligible_rows_remain()
            except Exception as exc:  # noqa: BLE001 - reported by category only
                self._fail(
                    totals, otp_accounting.failure_category(exc),
                    f'could not check whether eligible rows remain after batch {batch}',
                )
            if not any(remaining.values()):
                self._report_totals(totals)
                self.stdout.write('complete: no eligible rows found when last checked')
                return

        self._report_totals(totals)
        self.stdout.write('batch limit reached: eligible rows may remain')

    @staticmethod
    def _add(totals, counts):
        for table, deleted in counts.items():
            totals[table] += deleted

    def _report_incomplete_batch(self, batch, failure):
        parts = []
        for table in TABLES:
            if table in failure.completed:
                parts.append(f'{table} deleted {failure.completed[table]}')
            elif table == failure.failed_table:
                parts.append(f'{table} outcome unknown')
            else:
                parts.append(f'{table} not attempted')
        self.stdout.write(f'batch {batch} (incomplete): ' + ', '.join(parts))

    def _report_totals(self, totals):
        for table in TABLES:
            self.stdout.write(f'{table}: deleted {totals[table]}')

    def _fail(self, totals, category, what):
        self._report_totals(totals)
        # ``from None``: the database's exception, and its text, are not part of what an
        # operator is shown — not even with the default traceback-free output.
        raise CommandError(
            f'prune stopped (category={category}): {what}. The deletions printed above '
            'are confirmed; eligible rows may remain.'
        ) from None
