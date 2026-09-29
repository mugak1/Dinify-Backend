"""
Delete OTP accounting rows that are past retention (D11 B2-C).

A row is ELIGIBLE once it is strictly older than seven days, in any state. This command
removes eligible rows in bounded, oldest-first batches from both ledger tables and
prints COUNTS ONLY — never a key, an id or a timestamp. The retention is fixed in code
and cannot be overridden here, and nothing schedules this command: it runs when an
operator runs it.

    python manage.py prune_otp_accounting [--batch-size N] [--max-batches M]
"""
from django.core.management.base import BaseCommand, CommandError

from users_app import otp_accounting

DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 100
MAX_BATCHES = 10000


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

        totals = {'otp_issuances': 0, 'otp_verification_failures': 0}
        drained = False
        for _ in range(max_batches):
            counts = otp_accounting.prune(batch_size=batch_size)
            for table, deleted in counts.items():
                totals[table] += deleted
            if all(deleted < batch_size for deleted in counts.values()):
                drained = True
                break

        for table, deleted in totals.items():
            self.stdout.write(f'{table}: deleted {deleted}')
        if drained:
            self.stdout.write('complete: no eligible rows remained')
        else:
            self.stdout.write('batch limit reached: eligible rows may remain')
