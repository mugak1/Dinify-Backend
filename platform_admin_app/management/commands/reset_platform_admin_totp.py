from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import audit, sessions
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_RECOVERY_CODES_GENERATED,
    ADMIN_AUTH_TOTP_RESET,
    ADMIN_SESSION_REVOKED,
)
from platform_admin_app.management.commands._admin_bootstrap import (
    provision_credentials,
    require_encryption_key,
    write_enrolment_block,
)
from platform_admin_app.models import RESULT_SUCCESS, PlatformStaffAuth
from users_app.models import User


class Command(BaseCommand):
    help = (
        'Break-glass: re-provision the TOTP secret and recovery codes for an existing '
        'platform-staff account, revoking all of its sessions. Use when the '
        'authenticator device is lost or a secret may be compromised. The password is '
        'NOT changed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            required=True,
            help='The platform-staff account to re-provision.',
        )
        parser.add_argument(
            '--noinput',
            action='store_true',
            help='Skip the confirmation prompt (for a scripted recovery runbook).',
        )

    def handle(self, *args, **options):
        username = options['username'].strip()

        require_encryption_key()

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'No user with username {username!r}.')

        if user.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
            raise CommandError(
                f'{username!r} is not a platform-staff account; this command only '
                'reprovisions admin credentials.'
            )

        auth = PlatformStaffAuth.objects.filter(user=user).first()
        if auth is None:
            raise CommandError(
                f'{username!r} has no PlatformStaffAuth row to reprovision.'
            )

        if not options['noinput']:
            self.stdout.write(
                f'This invalidates the current authenticator and ALL recovery codes '
                f'for {username!r}, and signs out every active session.'
            )
            if input('Type the username to confirm: ').strip() != username:
                raise CommandError('Confirmation did not match; nothing was changed.')

        with transaction.atomic():
            uri, codes = provision_credentials(auth, username)
            revoked = sessions.revoke_all_for_user(user, 'totp reprovisioned')

            audit.record(
                action=ADMIN_AUTH_TOTP_RESET,
                result=RESULT_SUCCESS,
                actor=user,
                actor_label=username,
                resource_type='PlatformStaffAuth',
                resource_id=str(auth.id),
                reason='reset_platform_admin_totp management command',
            )
            audit.record(
                action=ADMIN_AUTH_RECOVERY_CODES_GENERATED,
                result=RESULT_SUCCESS,
                actor=user,
                actor_label=username,
                resource_type='PlatformStaffAuth',
                resource_id=str(auth.id),
                reason='reset_platform_admin_totp management command',
            )
            if revoked:
                audit.record(
                    action=ADMIN_SESSION_REVOKED,
                    result=RESULT_SUCCESS,
                    actor=user,
                    actor_label=username,
                    reason=f'{revoked} session(s) revoked by TOTP reprovisioning',
                )

        self.stdout.write(self.style.SUCCESS(
            f'Re-provisioned {username!r}; revoked {revoked} active session(s).'
        ))
        write_enrolment_block(self, uri, codes)
