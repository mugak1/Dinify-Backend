"""Export — or check — the committed D01 ceiling contract.

The ceilings live in ``order_input``; ``orders_app/contracts/checkout_limits.py``
derives the published set from them, and this command writes that derivation to disk
so a client repository has something stable to agree with.

    manage.py export_checkout_limits_contract            # report, change nothing
    manage.py export_checkout_limits_contract --check    # non-zero if stale
    manage.py export_checkout_limits_contract --write    # regenerate the file

It is a convenience, NOT the gate. The gate is
``orders_app/tests_order_input.py::CrossRepositoryCeilingContractTests``, which
asserts the committed file against the live constants unconditionally — so a ceiling
changed without regenerating fails CI whether or not anyone runs this.

It reads and writes exactly one file in this repository. It contacts nothing, touches
no database and makes no claim about any other repository's copy.
"""
from django.core.management.base import BaseCommand, CommandError

from orders_app.contracts import checkout_limits


class Command(BaseCommand):
    help = 'Export or check the committed D01 checkout-limits contract.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--check', action='store_true',
            help='Exit non-zero if the committed file is not what the constants produce.',
        )
        parser.add_argument(
            '--write', action='store_true',
            help='Regenerate the committed file from the constants.',
        )

    def handle(self, *args, **options):
        if options['check'] and options['write']:
            raise CommandError('--check and --write ask for opposite things; pick one.')

        expected = checkout_limits.export_text()
        current = (
            checkout_limits.CONTRACT_FILE.read_text()
            if checkout_limits.CONTRACT_FILE.is_file() else None
        )
        in_step = current == expected

        self.stdout.write(f'file    {checkout_limits.CONTRACT_FILE}')
        self.stdout.write(f'digest  {checkout_limits.contract_digest()}')
        self.stdout.write(f'state   {"in step" if in_step else "STALE"}')

        if options['write']:
            if in_step:
                self.stdout.write(self.style.SUCCESS('Already in step; nothing written.'))
                return
            checkout_limits.CONTRACT_FILE.write_text(expected)
            self.stdout.write(self.style.SUCCESS('Regenerated.'))
            return

        if options['check'] and not in_step:
            raise CommandError(
                'The committed contract is not what the constants produce. Run with '
                '--write, and remember the client repository pins this digest in its '
                'release policy — changing a ceiling is a two-repository change.'
            )
