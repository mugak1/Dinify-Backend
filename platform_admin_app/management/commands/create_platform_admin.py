import getpass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from platform_admin_app import audit
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_RECOVERY_CODES_GENERATED,
    ADMIN_AUTH_TOTP_ENROLLED,
)
from platform_admin_app.management.commands._admin_bootstrap import (
    provision_credentials,
    require_encryption_key,
    require_tty,
    write_enrolment_block,
)
from platform_admin_app.models import RESULT_SUCCESS
from platform_admin_app.services import promote_to_platform_staff
from users_app.models import User


class Command(BaseCommand):
    help = (
        'Create the first (or an additional) platform-staff account for the admin '
        'control plane: sets a password interactively, enrols TOTP, and prints the '
        'provisioning QR plus ten one-time recovery codes. The username must NOT be '
        'a phone number — the admin identity is deliberately separate from any '
        'restaurant account.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            required=True,
            help='Login name for the admin account (not a phone number).',
        )
        parser.add_argument(
            '--email',
            required=True,
            help='Contact email for the account.',
        )
        parser.add_argument(
            '--full-name',
            default='',
            help='Optional display name, e.g. "Ada Lovelace".',
        )

    def handle(self, *args, **options):
        username = options['username'].strip()
        email = options['email'].strip()
        full_name = options['full_name'].strip()

        # Refuse before touching the database — a half-provisioned admin account
        # that can never complete TOTP is worse than no account.
        require_encryption_key()
        require_tty()

        if username.replace('+', '').isdigit():
            raise CommandError(
                'The admin username must not be a phone number. Restaurant accounts '
                'use the phone number as their username; the admin identity must be '
                'separate and unmistakable.'
            )
        if User.objects.filter(username=username).exists():
            raise CommandError(f'A user with username {username!r} already exists.')
        if email and User.objects.filter(email=email).exists():
            raise CommandError(
                f'A user with email {email!r} already exists. The admin account needs '
                'its own address — the password-reset flow resolves users by email.'
            )

        password = getpass.getpass('Password for the new admin account: ')
        if not password:
            raise CommandError('No password entered; nothing was created.')
        if password != getpass.getpass('Repeat password: '):
            raise CommandError('Passwords did not match; nothing was created.')

        first_name, _, last_name = full_name.partition(' ')

        with transaction.atomic():
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                # Deliberately no phone number: the admin plane authenticates with a
                # password plus TOTP and must never depend on SMS.
                phone_number=None,
                first_name=first_name or None,
                last_name=last_name or None,
                # The only production write of User.roles, and it writes nothing:
                # a platform admin's authority comes from account_type, never from
                # a role string. `roles` carries restaurant roles only.
                roles=[],
                prompt_password_change=False,
            )
            # Routes through the invariant service rather than setting account_type
            # directly, so the "no active restaurant memberships" rule is enforced
            # on this path too.
            auth = promote_to_platform_staff(user)
            uri, codes = provision_credentials(auth, username)

            audit.record(
                action=ADMIN_AUTH_TOTP_ENROLLED,
                result=RESULT_SUCCESS,
                actor=user,
                actor_label=username,
                resource_type='PlatformStaffAuth',
                resource_id=str(auth.id),
                reason='create_platform_admin management command',
            )
            audit.record(
                action=ADMIN_AUTH_RECOVERY_CODES_GENERATED,
                result=RESULT_SUCCESS,
                actor=user,
                actor_label=username,
                resource_type='PlatformStaffAuth',
                resource_id=str(auth.id),
                reason='create_platform_admin management command',
            )

        self.stdout.write(self.style.SUCCESS(
            f'Created platform-staff account {username!r} ({user.id}).'
        ))
        write_enrolment_block(self, uri, codes)
