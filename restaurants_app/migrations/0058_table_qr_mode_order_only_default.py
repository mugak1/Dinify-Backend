"""
D07 — the default QR mode for a NEW table becomes ``order_only``.

MODEL STATE ONLY. One ``AlterField`` carrying the new default and the neutral
``order_pay`` label, and deliberately NO ``RunPython``: not one existing row is
read or rewritten.

WHY NO DATA MIGRATION. ``order_pay`` and ``order_only`` are operationally
identical — both sit in ``order_eligibility.ORDERING_QR_MODES``, so a diner may
order through either and nothing else in the tree branches on which one it is.
Rewriting stored rows would therefore change a tenant's recorded configuration
to no operational effect, which is an unforced edit of somebody else's data.
What was untruthful was the DEFAULT (a table nobody configured claimed in-app
payment collection) and the LABEL; both are fixed here, and neither needs a row
to move.

WHAT THIS DOES AND DOES NOT CHANGE AT THE DATABASE. Django manages ``default``
in Python, so an ``AlterField`` that changes one emits no DDL for it at all —
the column is not rewritten, not locked for a rewrite, and no value already
stored is touched. ``choices`` is likewise Python-side. The practical effect is
that an INSERT which does not name ``qr_mode`` now stores ``order_only``.

ROLLBACK IS SAFE AND IS A TRUE INVERSE. Under the expand-only rule a rollback
lands OLD CODE on the NEW schema: old code writing ``order_pay`` by default
still satisfies the column (the value remains in ``choices`` and in the ordering
whitelist), and rows written as ``order_only`` under this migration stay valid
to it for the same reason. Nothing is stranded in either direction.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants_app', '0057_restaurant_is_test'),
    ]

    operations = [
        migrations.AlterField(
            model_name='table',
            name='qr_mode',
            field=models.CharField(
                choices=[
                    ('menu_only', 'Menu Only'),
                    ('order_pay', 'Order (legacy)'),
                    ('order_only', 'Order Only'),
                ],
                default='order_only',
                max_length=20,
            ),
        ),
    ]
