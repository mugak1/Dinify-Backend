"""
Bring ONE pre-existing canonical ``Restaurant`` into the Admin onboarding domain as
``legacy_adopted`` provenance, with an audit trail.

A THIN ADAPTER. Every rule — target, actor, reason, locking, idempotency, the
provenance conflict, the owner-consistency precondition, the audit row — lives in
``platform_admin_app.onboarding_adoption.adopt_existing_restaurant``, which the
future Admin endpoint will call unchanged. This file translates shell arguments into
that call and its outcome into sentences an operator can act on. It deliberately
adds no policy of its own: a command that enforced a rule the service did not would
mean the shell and the portal could adopt different things.

WHY A COMMAND AND NOT AN ENDPOINT, YET. The permanent home for adoption is the Admin
portal, which does not have this screen. The alternative to a narrow, audited,
reviewable mechanism is not "wait" — it is somebody running
``RestaurantOnboarding.objects.create(...)`` in a ``shell_plus`` against production,
where nothing validates the shape, nothing checks that the owner of record and the
owner authority agree, and no row records who decided or why. This is the same
operation made attributable.

WHAT IT WILL NOT LET YOU DO, and each absence is the point:

  NO ``--source``. This command adopts; it can only ever write ``legacy_adopted``.
  Provenance is derived from the operation, never chosen by the caller — a flag that
  let an operator declare a tenant ``admin_created`` would make provenance a claim
  rather than a record.

  NO ``--restaurant-name``, no fuzzy match, no bulk mode. One invocation, one
  immutable UUID. The wrong-tenant failure here is silent: nothing errors, the
  restaurant keeps working, and its recorded history is permanently wrong.

  NO ``--attest-owner``. Adoption is not a personal verification that the owner
  controls the account, and the flag would make it trivially easy to record that it
  was. Attestation is a separate audited decision, and this command leaves all three
  attestation columns NULL.

  NO ``--invite-owner``, ``--send-email``, ``--send-sms``. A legacy tenant did not
  enter Dinify through the invitation system; no ``OwnerInvitation`` is created and
  nothing is delivered to anybody.

IT DOES NOT TOUCH THE RESTAURANT. Not the lifecycle state, not the test
classification, not the owner, not employees, menus, tables, QR state or orders. The
whole write is one ``RestaurantOnboarding`` row plus its audit entry.

REFUSALS ARE NOT AUDITED, matching ``mark_restaurant_test`` and differing from the
HTTP transition endpoint on purpose. There, a refusal is an authenticated
administrator being told no. Here the commonest refusal is an actor that could not be
resolved — there is nobody to attribute a row to — and writing denial rows from an
unauthenticated shell would let anyone with box access spray the audit log with
attribution nobody stood behind. The operator sees the error immediately, and no
state changed.
"""
from django.core.management.base import BaseCommand, CommandError

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app.onboarding import OwnerConsistencyError
from platform_admin_app.onboarding_adoption import (
    AdoptionError,
    adopt_existing_restaurant,
)
from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
from users_app.models import User

# What each owner-consistency code means for an operator, and what to do about it.
# The service refuses and never repairs, so the only useful thing a command can add
# is the next step — deliberately phrased as work for a human, since every one of
# these is a question about who runs a business.
OWNER_CONSISTENCY_ADVICE = {
    'missing_owner_membership': (
        'The restaurant has no active owner-role employee record, so nobody holds '
        'owner authority in the portal. Resolve the ownership first, then re-run.'
    ),
    'multiple_owner_memberships': (
        'More than one active employee carries the owner role, so who owns this '
        'restaurant is ambiguous. A human must decide which is correct before it '
        'can be adopted.'
    ),
    'owner_membership_mismatch': (
        'The owner of record (Restaurant.owner) and the active owner-role employee '
        'are different people. Resolve which is correct, then re-run.'
    ),
}


def _resolve_actor(raw):
    """
    The platform-staff ``User`` named by ``--actor``, or a ``CommandError``.

    Resolved by USERNAME here because that is what an operator has to hand; the
    service is handed the instance and re-validates it against the row, so this is a
    lookup convenience and not the eligibility check. It still fails on all three
    counts, so a mistyped or ineligible actor is refused with a sentence naming the
    argument at fault rather than a service error about an object the operator never
    passed.

    ATTRIBUTION, NOT AUTHENTICATION: anyone who can run ``manage.py`` on the box
    already has more authority than this command grants. ``--actor`` is what puts a
    real name on the audit row. No password, TOTP code or recovery code is asked for,
    and none is printed.
    """
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise CommandError('--actor is required.')

    try:
        actor = User.objects.get(username=cleaned)
    except User.DoesNotExist:
        raise CommandError(f'No user with username {cleaned!r}.')

    if actor.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
        raise CommandError(
            f'{cleaned!r} is not a platform-staff account. Adoption is a platform '
            'decision; a restaurant user can never be its actor.'
        )
    if not actor.is_active:
        raise CommandError(
            f'{cleaned!r} is deactivated and cannot be recorded as the actor.'
        )
    return actor


class Command(BaseCommand):
    help = (
        'Represent exactly one pre-existing restaurant, identified by UUID, in the '
        'Admin onboarding domain as legacy_adopted provenance — attributed to a '
        'platform-staff actor and recorded in the admin audit log. Creates one '
        'RestaurantOnboarding row and nothing else; the restaurant itself is not '
        'modified, no owner invitation is created and no owner control is attested.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--restaurant',
            required=True,
            help='The target restaurant UUID. Names are never accepted.',
        )
        parser.add_argument(
            '--actor',
            required=True,
            help='Username of the platform-staff human making this decision.',
        )
        parser.add_argument(
            '--reason',
            required=True,
            help=(
                'Why this restaurant is being adopted into the Admin onboarding '
                f'domain. At least {MIN_REASON_LENGTH} characters after trimming.'
            ),
        )

    def handle(self, *args, **options):
        actor = _resolve_actor(options['actor'])

        try:
            result = adopt_existing_restaurant(
                restaurant_id=options['restaurant'],
                actor=actor,
                reason=options['reason'],
            )
        except OwnerConsistencyError as error:
            # Kept as its own failure rather than folded into AdoptionError: this is
            # not an invalid adoption, it is a restaurant whose ownership two systems
            # disagree about, and the remedy is different for each code. Details are
            # UUIDs only, so this is safe to print.
            advice = OWNER_CONSISTENCY_ADVICE.get(error.code, '')
            raise CommandError(
                f'Refused: owner consistency check failed ({error.code}). {advice} '
                'Nothing was changed and no ownership was repaired.'
            )
        except AdoptionError as error:
            raise CommandError(f'Refused ({error.code}): {error.message}')

        self._report(result=result, actor=actor)

    def _report(self, *, result, actor):
        """
        Say plainly what happened. Restaurant name and UUID only — never the owner's
        name, email or phone, never credentials, and never unrelated tenant data.
        """
        restaurant = result.restaurant
        onboarding = result.onboarding

        if not result.created:
            self.stdout.write('\n'.join([
                f'Restaurant:                {restaurant.name}',
                f'UUID:                      {restaurant.id}',
                f'Onboarding source:         {onboarding.source}',
                f'Originally adopted at:     {onboarding.adopted_at}',
                f'Result:                    already adopted',
            ]))
            self.stdout.write(
                '\nRestaurant is already represented in Admin onboarding as '
                'legacy_adopted. No changes made.'
            )
            return

        self.stdout.write('\n'.join([
            f'Restaurant:                {restaurant.name}',
            f'UUID:                      {restaurant.id}',
            f'Onboarding source:         {onboarding.source}',
            'Owner consistency:         verified',
            'Owner control attestation: not recorded',
            'Invitation:                none',
            f'Audit actor:               {actor.username}',
            'Result:                    adopted',
        ]))
        self.stdout.write(self.style.SUCCESS(
            '\nCreated one RestaurantOnboarding row and wrote one '
            'admin.restaurant.onboarding_adopted audit entry. The restaurant, its '
            'owner, its employees and all of its operational data are unchanged.'
        ))
