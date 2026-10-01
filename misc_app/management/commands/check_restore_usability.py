"""The thin adapter the restore-usability bootstrap hands over to (D15 R3).

It holds NO policy: every precondition, read and check lives in
``misc_app.restore_usability``. Its one rule is to refuse anything that did not come
through that module's bootstrap::

    python -m misc_app.restore_usability --inputs INPUTS.json --result RESULT.json --nonce HEX

``manage.py check_restore_usability`` REFUSES (exit 3). By the time any command code
runs, ``manage.py`` has already imported the settings and initialised Django, so checking
isolation here could only happen AFTER the application started — that is not pre-start
protection, and this command never presents it as such. Nor is an environment variable
accepted as proof that the bootstrap ran: the handover is an in-process object the
bootstrap issues after its pre-start phase, registered with it and accepted once.
"""
from django.core.management.base import BaseCommand, CommandError

from misc_app import restore_usability

REFUSAL = (
    'refused: this command runs only as the handover from '
    '`python -m misc_app.restore_usability`. Invoked any other way the settings and the '
    'application have already started, so it cannot provide pre-start protection.'
)


class Command(BaseCommand):
    help = (
        'Restore-usability check (D15 R3) — ADAPTER ONLY. Run '
        '`python -m misc_app.restore_usability --inputs INPUTS.json --result RESULT.json '
        '--nonce HEX` from an isolated source checkout; see that module for the inputs '
        'schema and the supported profile. Invoked directly this command refuses '
        '(exit 3) and checks nothing.'
    )
    # Never run Django's system checks or the migration check here: both touch the
    # database before the bootstrap's own read-only checks have run.
    requires_system_checks = []
    requires_migrations_checks = False
    # The handover travels in process only; it is deliberately not a command-line option.
    stealth_options = ('bootstrap',)

    def handle(self, *args, **options):
        try:
            proof = restore_usability.accept_proof(options.get('bootstrap'))
        except restore_usability.BootstrapRefused as exc:
            raise CommandError(f'{REFUSAL} ({exc})', returncode=restore_usability.EXIT_REFUSED)
        restore_usability.run_checks(proof)
