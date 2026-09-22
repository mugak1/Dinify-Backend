"""Export — or check — the committed published-capabilities contract.

``orders_app/contracts/published_capabilities.py`` derives the published levels from
the constants the wire emits; this command writes that derivation to disk so a
client repository's peer-receipt producer has something stable to read at an exact
commit.

    manage.py export_published_capabilities            # report, change nothing
    manage.py export_published_capabilities --check    # non-zero if stale
    manage.py export_published_capabilities --write    # regenerate the file

A convenience, NOT the gate: ``orders_app/tests_published_capabilities.py`` asserts
the committed file against the live constants unconditionally, so a level raised
without regenerating fails CI whether or not anyone runs this.

It reads and writes exactly one file in this repository. It contacts nothing,
touches no database and makes no claim about what any deployment is serving.
"""
from django.core.management.base import BaseCommand, CommandError

from orders_app.contracts import published_capabilities


class Command(BaseCommand):
    help = 'Export or check the committed published-capabilities contract.'

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

        expected = published_capabilities.export_text()
        current = (
            published_capabilities.CONTRACT_FILE.read_text()
            if published_capabilities.CONTRACT_FILE.is_file() else None
        )
        in_step = current == expected

        self.stdout.write(f'file    {published_capabilities.CONTRACT_FILE}')
        self.stdout.write(f'digest  {published_capabilities.contract_digest()}')
        self.stdout.write(f'state   {"in step" if in_step else "STALE"}')

        if options['write']:
            if in_step:
                self.stdout.write(self.style.SUCCESS('Already in step; nothing written.'))
                return
            published_capabilities.CONTRACT_FILE.write_text(expected)
            self.stdout.write(self.style.SUCCESS('Regenerated.'))
            return

        if options['check'] and not in_step:
            raise CommandError(
                'The committed capabilities export is not what the constants produce. '
                'Run with --write. A client repository selects this backend by commit '
                'through a peer receipt read from this file, so a raised level only '
                'reaches its release gate once a receipt for the new commit is produced '
                'and approved there.'
            )
