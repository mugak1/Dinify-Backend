"""
System checks: say so, in every deploy log, while a dining-model switch is off.

W001 is the visit writer (Step 1); W002 is round holding (Step 2, PR BE-F), below.

`DINING_VISIT_WRITES_ENABLED` (settings.py) decides whether accepting an order also
writes its visit, bill and placement (`orders_app/controllers/services/
round_placement.py`). It is ON by default and exists only as the remedy for a writer
fault: while it is off, orders are still accepted, and every order accepted in that
time has no visit. Nothing about that is visible to a diner, the kitchen or a report,
so it is easy to leave off and forget.

So this is a WARNING, never an Error. `deploy-uat.yml` runs `manage.py migrate` and
`manage.py check --deploy` (twice: as `ubuntu` and as `www-data`) under `set -e`;
both print warnings and fail only on errors. A switched-off writer is therefore
printed on every deploy (under the run's "remote stderr" section) without ever
stopping one. It is registered
as an ordinary check, not deploy-only, so `manage.py check` and `migrate` show it
too.

Same shape as `restaurants_app/checks.py`: a pure predicate that takes plain
arguments, so tests exercise the decision directly, and a thin registered wrapper
that reads the live setting.
"""
from django.conf import settings
from django.core.checks import Warning, register

CHECK_ID = 'orders_app.W001'
HOLDS_CHECK_ID = 'orders_app.W002'


def check_dining_visit_writes(enabled):
    """The check messages for the switch's value. Pure: reads no settings."""
    if enabled:
        return []
    return [
        Warning(
            'DINING_VISIT_WRITES_ENABLED is off: accepted orders are getting no '
            'visit, bill or placement.',
            hint=(
                'Orders accepted while it is off have no visit, and nothing '
                'backfills them. Set DINING_VISIT_WRITES_ENABLED=true in the project '
                '.env and re-run "Deploy UAT Backend" for the deployed commit to '
                'turn it back on. An empty value also means off.'
            ),
            id=CHECK_ID,
        )
    ]


@register()
def dining_visit_writes_enabled(app_configs, **kwargs):
    """Wire the pure predicate to the live setting."""
    return check_dining_visit_writes(
        getattr(settings, 'DINING_VISIT_WRITES_ENABLED', True),
    )


# --- W002: round holding (Step 2, PR BE-F) ------------------------------------------
#
# `DINING_ROUND_HOLDS_ENABLED` (settings.py) decides whether a diner's round at a
# pay-after restaurant is HELD at acceptance until staff Confirm it on the floor. It
# is OFF by default, the opposite of W001's switch, so this warning prints on every
# deploy until the owner turns holding on: the deploy log then says, either way,
# whether holding is live. A WARNING, never an Error, for the reason above: off is a
# legitimate, deliberate state, and a check must never stop a deploy over it.

def check_dining_round_holds(enabled):
    """The check messages for the holds switch's value. Pure: reads no settings."""
    if enabled:
        return []
    return [
        Warning(
            'DINING_ROUND_HOLDS_ENABLED is off: pay-after rounds are released at '
            'acceptance, not held for staff.',
            hint=(
                'Set DINING_ROUND_HOLDS_ENABLED=true in the project .env and re-run '
                '"Deploy UAT Backend" for the deployed commit to hold diner rounds '
                'at pay-after restaurants. An empty value also means off.'
            ),
            id=HOLDS_CHECK_ID,
        )
    ]


@register()
def dining_round_holds_enabled(app_configs, **kwargs):
    """Wire the pure predicate to the live setting."""
    return check_dining_round_holds(
        getattr(settings, 'DINING_ROUND_HOLDS_ENABLED', False),
    )
