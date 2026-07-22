"""
Data migration: flip existing platform-role holders to account_type='platform_staff'.

Selects users whose ``roles`` contains 'dinify_admin' or 'dinify_account_manager'
and sets their ``account_type``. Uses Python-side iteration (NOT a
``roles__contains`` JSONField lookup) deliberately: ``__contains`` on a JSONField
raises ``NotSupportedError`` on SQLite, and migrations run during SQLite test-DB
setup — Python iteration is backend-independent and the user table is tiny. Role
strings are frozen literals here (migrations must not import runtime constants).
Reversible: the reverse sets the same role-holders back to 'restaurant_user'. No
enforcement, no membership or role changes.
"""
from django.db import migrations

# Frozen at migration time — mirror dinify_backend.configss.string_definitions
# DINIFY_ADMIN / DINIFY_ACCOUNT_MANAGER without importing the (mutable) module.
PLATFORM_ROLES = frozenset({'dinify_admin', 'dinify_account_manager'})


def _platform_role_holder_ids(User):
    return [
        u.pk
        for u in User.objects.all().only('id', 'roles')
        if isinstance(u.roles, list) and PLATFORM_ROLES.intersection(u.roles)
    ]


def flip_forward(apps, schema_editor):
    User = apps.get_model('users_app', 'User')
    ids = _platform_role_holder_ids(User)
    if ids:
        User.objects.filter(pk__in=ids).update(account_type='platform_staff')


def flip_reverse(apps, schema_editor):
    User = apps.get_model('users_app', 'User')
    ids = _platform_role_holder_ids(User)
    if ids:
        User.objects.filter(pk__in=ids).update(account_type='restaurant_user')


class Migration(migrations.Migration):

    dependencies = [
        ('users_app', '0010_user_account_type'),
    ]

    operations = [
        migrations.RunPython(flip_forward, flip_reverse),
    ]
