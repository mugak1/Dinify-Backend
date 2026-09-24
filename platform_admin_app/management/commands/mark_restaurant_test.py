"""
Set or clear a restaurant's platform-owned TEST CLASSIFICATION, with an audit trail.

``Restaurant.is_test`` marks a tenant that exists for Dinify's own testing or
demonstration rather than to trade. Orders placed while it is set are FLAGGED as test
orders — ``Order.is_test`` is derived from it under the order-admission advisory lock —
and flagged is all: a test restaurant can do everything a live restaurant can, and its
flagged orders count in its own figures, can be reviewed and are matched to customers.
What the flag decides is what happens once the restaurant is REAL — its flagged orders
are then PRACTICE orders and leave its figures (``orders_app.controllers.test_orders``),
so flipping it back to real takes the orders it took as a test restaurant out of its
numbers — and whether Dinify's OWN portfolio and financial figures count the
restaurant at all: the Admin spec (§11, §16) leaves test restaurants out of them. Those
figures are not built yet, and they are Dinify's numbers, never the restaurant's.
Migration 0057 added the column with NO backfill and, in particular, no
name-based heuristic: a restaurant is not a test tenant because its name looks like
one. Deciding that a given tenant IS one is a commercial judgement a human makes.

WHY A COMMAND AND NOT AN ENDPOINT. The permanent home for this is the admin portal,
which does not exist yet. The alternative to a narrow, audited, reviewable mechanism
is not "wait" — it is somebody running a `Restaurant.objects.filter(...).update()` in
a `shell_plus` against production, which leaves no actor, no reason and no record.
This command is the same decision made attributable.

THREE PROPERTIES IT EXISTS TO GUARANTEE:

  UUID-ONLY TARGETING. The target is an immutable primary key, never a name. Name
  matching is exactly how the wrong tenant gets flagged, and the failure is silent:
  the orders keep working, they are just flagged test — and they leave the real
  restaurant's figures as soon as the mistake is corrected. There is deliberately no
  ``--restaurant-name``, no fuzzy match, no ``.first()``, and no bulk mode — one
  invocation, one row.

  ATTRIBUTION. ``--actor`` names the platform-staff human whose decision this is. It
  is NOT authentication: anyone who can run ``manage.py`` on the box already has
  more authority than this command grants. It is what puts a real name on the
  ``AdminAuditLog`` row, and it is validated (exists, ``platform_staff``, active) so
  the row cannot name somebody who could not have made the decision.

  ATOMICITY, AND THE ADMISSION BARRIER. The write and its audit entry share one
  transaction, per the no-audit-no-action half of the contract in
  ``platform_admin_app.audit``: a classification that cannot be attributed must not
  be allowed to stand. That transaction takes the EXCLUSIVE admission advisory lock
  first and the ``Restaurant`` row lock second — the same order
  ``lifecycle.transition_restaurant`` uses — so an order cannot be admitted against
  one classification and then written under the other.

IT IS BIDIRECTIONAL AND IDEMPOTENT. ``--test false`` is a first-class operation, not
an afterthought — a mistaken classification has to be correctable by the same audited
path that made it, or the correction happens in a shell instead. And re-running the
same classification writes nothing and audits nothing: the log records DECISIONS that
changed platform state, not the number of times a runbook was pasted.

WHAT IT DOES NOT DO. The write is ``is_test`` plus the row's ``auto_now``
``time_last_updated`` stamp, and nothing else — not ``status`` (that is
``restaurants_app.controllers.lifecycle``'s sole privilege), not the owner, not the
soft-delete flag. A test asserts that column set exactly, rather than trusting a
list of fields somebody remembered to worry about.

Above all it does NOT rewrite history: existing ``Order.is_test`` rows are left
exactly as they are. Classification governs how FUTURE orders are flagged, at
admission time; retro-labelling orders that were placed under a different
classification would silently restate past revenue, which is a separate decision
needing its own reasoning and its own migration. (Whether an already-flagged order
COUNTS follows the restaurant's current classification — see
``orders_app.controllers.test_orders`` — which is a reading rule, not a rewrite.)

REFUSALS ARE NOT AUDITED, deliberately, and this differs from the HTTP transition
endpoint on purpose. There, a refusal is an authenticated administrator being told no
— worth recording. Here, the commonest refusal is an actor that could not be
resolved, so there is nobody to attribute a row to; writing denial rows from an
unauthenticated shell would let anyone with box access spray the audit log with
attribution nobody stood behind. The operator sees the error immediately, and no
state changed.
"""
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import audit
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED,
)
from platform_admin_app.models import RESULT_SUCCESS
from restaurants_app.controllers.admission_lock import lock_admission_exclusive
from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
from restaurants_app.models import Restaurant
from users_app.models import User

# The ONLY accepted spellings for --test. A closed vocabulary rather than a truthiness
# test: '1', 'yes', 'on', 'y' and '' would each have to be GUESSED at, and the guess
# decides whether a tenant's trading counts as revenue. Case is folded (see
# `_canonical_test_value`) so `True` from a Python caller lands here unchanged in
# meaning; nothing else is accepted.
TRUE_VALUE = 'true'
FALSE_VALUE = 'false'
TEST_VALUES = (TRUE_VALUE, FALSE_VALUE)


def _canonical_test_value(raw):
    """
    argparse ``type`` for ``--test``: trim and fold case before ``choices`` decides.

    Deliberately does no interpretation of its own — it normalises spelling so that
    ``TRUE`` and ``true`` are the same word, and leaves the accept/reject decision to
    the ``choices`` list, which is the thing ``--help`` prints.
    """
    return str(raw).strip().lower()


def _parse_test_flag(raw):
    """
    The requested classification as a bool, or a ``CommandError``.

    RE-VALIDATED HERE rather than trusted from argparse, because the two invocation
    modes do not agree. ``call_command(..., test='TRUE')`` does push the value
    through the parser — so ``choices`` still rejects nonsense either way — but then
    overwrites the parsed result with the caller's raw keyword, so the ``type``
    conversion never reaches ``handle()``. Folding once, here, is what makes the
    shell and an in-process caller mean the same thing by the same word; without it
    ``'TRUE'`` would quietly classify a tenant as NOT a test tenant.
    """
    text = _canonical_test_value(raw)
    if text not in TEST_VALUES:
        raise CommandError(
            f'--test must be exactly {TRUE_VALUE!r} or {FALSE_VALUE!r} '
            f'(got {raw!r}). No truthiness is inferred: this flag decides whether a '
            "tenant's orders count as commerce, so it has to be stated."
        )
    return text == TRUE_VALUE


def _validate_reason(raw):
    """
    The trimmed operator reason, or a ``CommandError``.

    The length bar is IMPORTED from ``restaurants_app.controllers.lifecycle``, the
    sibling operation — the other audited, reason-required, platform-owned write to
    this same row. Two admin actions on one restaurant disagreeing about how much of
    a reason a reason has to be would be arbitrary. (That constant in turn mirrors
    ``platform_admin_app.delegation.MIN_REASON_LENGTH``, so all three agree at 10.)

    Only the CONSTANT is reused: ``lifecycle._validate_reason`` raises a
    ``LifecycleTransitionError`` carrying an endpoint-shaped ``errors`` dict, which
    is the wrong failure for a shell command.
    """
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise CommandError('--reason is required and cannot be blank.')
    if len(cleaned) < MIN_REASON_LENGTH:
        raise CommandError(
            f'--reason must be at least {MIN_REASON_LENGTH} characters '
            f'(got {len(cleaned)}). State why this tenant is being reclassified — '
            'the audit row is only as useful as this sentence.'
        )
    return cleaned


def _parse_restaurant_id(raw):
    """
    The target's UUID, or a ``CommandError``.

    Parsed strictly, and BEFORE any database access: a malformed identifier is an
    operator typo, and letting it reach a queryset would surface as a ValidationError
    from deep inside the ORM instead of a sentence naming the argument at fault.
    """
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise CommandError('--restaurant is required.')
    try:
        return uuid.UUID(cleaned)
    except ValueError:
        raise CommandError(
            f'--restaurant must be a restaurant UUID; {cleaned!r} is not one. '
            'This command targets an immutable primary key, never a name.'
        )


def _resolve_actor(raw):
    """
    The platform-staff ``User`` whose decision this is, or a ``CommandError``.

    Fails closed on all three counts — unknown, wrong plane, deactivated — before
    anything is written. The row must not be able to name an account that could not
    have made the decision it records.
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
            f'{cleaned!r} is not a platform-staff account. Test classification is a '
            'platform decision; a restaurant user can never be its actor.'
        )
    if not actor.is_active:
        raise CommandError(
            f'{cleaned!r} is deactivated and cannot be recorded as the actor.'
        )
    return actor


class Command(BaseCommand):
    help = (
        'Set or clear Restaurant.is_test for exactly one restaurant, identified by '
        'UUID, attributed to a platform-staff actor and recorded in the admin audit '
        'log. Writes only that one column; existing Order.is_test rows are untouched.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--restaurant',
            required=True,
            help='The target restaurant UUID. Names are never accepted.',
        )
        parser.add_argument(
            '--test',
            required=True,
            type=_canonical_test_value,
            choices=TEST_VALUES,
            help=(
                "'true' marks the restaurant a test tenant; 'false' clears it. "
                'Nothing else is accepted — no truthiness is inferred.'
            ),
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
                'Why this tenant is being reclassified. At least '
                f'{MIN_REASON_LENGTH} characters after trimming.'
            ),
        )

    def handle(self, *args, **options):
        # Cheap, purely local validation first — nothing here needs the database, and
        # a typo should never reach a row lock.
        requested = _parse_test_flag(options['test'])
        reason = _validate_reason(options['reason'])
        restaurant_id = _parse_restaurant_id(options['restaurant'])
        actor = _resolve_actor(options['actor'])

        with transaction.atomic():
            # THE ADMISSION BARRIER, taken FIRST — before the row lock below, and
            # for the same reason `lifecycle.transition_restaurant` takes it first:
            # the advisory lock is the single TOP level of the documented order
            # (advisory -> Restaurant -> AdminAuditLog), and the order paths take
            # the shared side of it first too.
            #
            # THE ROW LOCK ALONE WOULD NOT DO THIS JOB, which is easy to get wrong
            # because it looks like it should. `order_admission.admit` reads
            # `is_test` with a PLAIN `values_list().get()` under the shared advisory
            # lock — it never row-locks the restaurant — and under PostgreSQL's MVCC
            # a plain SELECT does not block on a row somebody else holds FOR UPDATE.
            # So without this line an admission could read the old flag, this
            # transaction could commit the new one, and the order could then be
            # INSERTed with the obsolete classification: a real sale flagged test,
            # which leaves the restaurant's figures once it is real, with no error
            # anywhere.
            # `_create_order` states that "the lock is what makes both values still
            # true at the INSERT" — that guarantee was vacuous only while nothing
            # wrote the flag, and this command is the first writer.
            lock_admission_exclusive(restaurant_id)

            restaurant = (
                Restaurant.objects
                .select_for_update()
                .filter(pk=restaurant_id)
                .first()
            )
            # ONE message for "missing" and "soft-deleted" alike, mirroring the admin
            # plane's `_not_found()`: a soft-deleted tenant is not a target, and this
            # command has no business confirming it ever existed.
            if restaurant is None or restaurant.deleted:
                raise CommandError(
                    f'No restaurant with id {restaurant_id}. (A soft-deleted '
                    'restaurant is not a valid target and is reported the same way.)'
                )

            current = bool(restaurant.is_test)
            changed = current != requested

            if changed:
                restaurant.is_test = requested
                # `time_last_updated` is auto_now, so Django stamps it itself — but a
                # field omitted from update_fields is not written, so it has to be
                # named. Those two are the whole write: no other column is touched.
                restaurant.save(update_fields=['is_test', 'time_last_updated'])

                # Inside the transaction, so a failed audit write unwinds the
                # classification it would have described. The state blobs carry the
                # classification fact and nothing else — no owner, no contact
                # details, no credentials; the UUID is the identity.
                audit.record(
                    action=ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED,
                    result=RESULT_SUCCESS,
                    actor=actor,
                    actor_label=actor.username,
                    resource_type='Restaurant',
                    resource_id=str(restaurant.id),
                    restaurant_id=restaurant.id,
                    reason=reason,
                    before_state={'is_test': current},
                    after_state={'is_test': requested},
                )

        self._report(
            restaurant=restaurant, actor=actor, current=current,
            requested=requested, changed=changed,
        )

    def _report(self, *, restaurant, actor, current, requested, changed):
        """
        Say plainly what happened. Restaurant name and UUID only — never owner or
        diner data, and never settings or credentials.
        """
        def word(value):
            return TRUE_VALUE if value else FALSE_VALUE

        classification = (
            f'{word(current)} -> {word(requested)}' if changed
            else f'{word(current)} (unchanged)'
        )
        lines = [
            f'Restaurant:     {restaurant.name}',
            f'UUID:           {restaurant.id}',
            f'Classification: {classification}',
            f'Audit actor:    {actor.username}',
            f'Result:         {"changed" if changed else "no-op"}',
        ]
        self.stdout.write('\n'.join(lines))

        if changed:
            self.stdout.write(self.style.SUCCESS(
                f'\nMarked is_test={word(requested)} and wrote one '
                f'{ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED} audit entry. '
                'Existing orders were NOT reclassified.'
            ))
        else:
            self.stdout.write(
                f'\nRestaurant already has is_test={word(requested)}; '
                'no changes made.'
            )
