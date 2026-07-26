"""
Clear a platform-staff lockout from the shell.

The narrow tool for the job. Before this existed the only ways out of a lockout were
a fully successful sign-in, waiting for the window to lapse (which does NOT reset the
cumulative counter, so the next failure re-locks straight away), or
``reset_platform_admin_totp`` — which destroys the authenticator, invalidates all ten
recovery codes and revokes every session. Reaching for that to undo a nuisance lockout
is a hammer for a latch.

Requires NO encryption key: it touches only the two counters, never the TOTP secret.
So it works in exactly the situation where the Fernet key has also been lost.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import audit, lockout
from platform_admin_app.audit_actions import ADMIN_AUTH_LOCKOUT_CLEARED
from platform_admin_app.models import RESULT_SUCCESS, PlatformStaffAuth
from users_app.models import User


class Command(BaseCommand):
    help = (
        'Clear the failed-attempt counter and any active lockout for a platform-staff '
        'account. Does NOT change the password, the TOTP secret or the recovery codes, '
        'and does not need ADMIN_SECRET_ENCRYPTION_KEY.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            required=True,
            help='The platform-staff account to unlock.',
        )

    def handle(self, *args, **options):
        username = options['username'].strip()

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'No user with username {username!r}.')

        if user.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
            raise CommandError(
                f'{username!r} is not a platform-staff account; this command only '
                'clears admin lockouts.'
            )

        with transaction.atomic():
            auth = (
                PlatformStaffAuth.objects
                .select_for_update()
                .filter(user=user)
                .first()
            )
            if auth is None:
                raise CommandError(
                    f'{username!r} has no PlatformStaffAuth row to unlock.'
                )

            was_locked = lockout.is_locked(auth)
            attempts = auth.failed_attempts or 0
            lockout.reset(auth)

            audit.record(
                action=ADMIN_AUTH_LOCKOUT_CLEARED,
                result=RESULT_SUCCESS,
                actor=user,
                actor_label=username,
                resource_type='PlatformStaffAuth',
                resource_id=str(auth.id),
                reason='unlock_platform_admin management command',
                before_state={'failed_attempts': attempts, 'was_locked': was_locked},
                after_state={'failed_attempts': 0, 'was_locked': False},
            )

        if attempts or was_locked:
            self.stdout.write(self.style.SUCCESS(
                f'Cleared {attempts} failed attempt(s) for {username!r}; '
                f'{"lock lifted" if was_locked else "no active lock"}.'
            ))
        else:
            self.stdout.write(
                f'{username!r} was not locked and had no failed attempts.'
            )
