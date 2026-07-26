"""
Data migration: close the two standing residues of ambient admin authority.

1. Blacklist every outstanding refresh token belonging to a ``platform_staff``
   account. Customer login, password reset and (as of this PR) the refresh route
   all refuse platform staff, but a token minted BEFORE those gates existed stays
   valid for the rest of its refresh lifetime. Revoking here means the new gate
   has no grandfathered exceptions.

2. Strip platform-only role strings from ``restaurant_user`` accounts, so the
   data matches the invariant the write paths now enforce: ``account_type``
   determines the plane, ``roles`` carries restaurant roles only.

   Deliberately scoped to ``restaurant_user`` rows. ``platform_staff`` rows keep
   their legacy role strings: migration 0011 identifies its subjects BY those
   strings when reversing, and clearing them would leave 0011's reverse unable to
   find the accounts it flipped. Those strings grant nothing — no code reads them
   any more — so leaving them costs nothing and keeps the history honest.

Both steps are idempotent, re-runnable, and no-ops on an empty set. The reverse is
a noop: un-blacklisting a token would be a security regression, and a stripped
role string is not information worth restoring (nothing consumes it).

Role strings are frozen literals here — migrations must not import runtime
constants, and in this case the constants were deleted outright.
"""
from django.db import migrations

# Mirrors platform_admin_app.services.PLATFORM_ONLY_ROLES, frozen at migration time.
PLATFORM_ROLES = frozenset({'dinify_admin', 'dinify_account_manager'})

ACCOUNT_TYPE_PLATFORM_STAFF = 'platform_staff'
ACCOUNT_TYPE_RESTAURANT_USER = 'restaurant_user'


def blacklist_platform_staff_tokens(apps, schema_editor):
    """Blacklist every outstanding refresh token held by a platform-staff account."""
    OutstandingToken = apps.get_model('token_blacklist', 'OutstandingToken')
    BlacklistedToken = apps.get_model('token_blacklist', 'BlacklistedToken')

    already_blacklisted = set(
        BlacklistedToken.objects.values_list('token_id', flat=True)
    )
    outstanding = OutstandingToken.objects.filter(
        user__account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    ).values_list('pk', flat=True)

    BlacklistedToken.objects.bulk_create([
        BlacklistedToken(token_id=token_id)
        for token_id in outstanding
        if token_id not in already_blacklisted
    ])


def strip_platform_roles(apps, schema_editor):
    """Remove platform-only role strings from restaurant_user accounts."""
    User = apps.get_model('users_app', 'User')

    # Python-side iteration, NOT a ``roles__contains`` JSONField lookup:
    # ``__contains`` raises NotSupportedError on SQLite and migrations run during
    # SQLite test-DB setup. Backend-independent, and the user table is tiny.
    for user in User.objects.filter(
        account_type=ACCOUNT_TYPE_RESTAURANT_USER,
    ).only('id', 'roles'):
        roles = user.roles
        if not isinstance(roles, list):
            continue
        cleaned = [role for role in roles if role not in PLATFORM_ROLES]
        if cleaned != roles:
            User.objects.filter(pk=user.pk).update(roles=cleaned)


def forward(apps, schema_editor):
    blacklist_platform_staff_tokens(apps, schema_editor)
    strip_platform_roles(apps, schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ('users_app', '0012_alter_user_phone_number'),
        # Only that the blacklist tables exist. Pinned to 0001_initial rather than
        # the current head so a simplejwt upgrade that adds migrations cannot
        # strand this one on a name that no longer exists.
        ('token_blacklist', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
