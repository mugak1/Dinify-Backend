"""
The onboarding + owner-control READ projection on the Admin restaurant detail
endpoint (Phase 1, Step 2C).

The projection's job is to answer "has anyone established that this owner controls
this restaurant?" without ever guessing, so the suite is organised around the ways a
read could come to claim more than the database can prove:

  IT COULD INFER CONTROL FROM AN ACTIVE ACCOUNT. Every signal that is easy to reach
  for and means something else — ``last_login``, ``is_active``, a password state, an
  owner FK, an owner membership, prior orders — is pinned to establishing nothing
  (``ControlIsNeverInferredTests``). This is the test that matters most: each of
  those would produce a confident answer, and a wrong one.

  IT COULD LET EVIDENCE OUTLIVE ITS SUBJECT. An attestation certifies ONE person.
  ``StaleAttestationTests`` reassigns the owner and proves the replacement does not
  inherit it — the reason Step 2A stored the attested subject at all. The invitation
  half of the same rule is ``AdminCreatedInvitationTests``.

  IT COULD TURN DRIFT INTO A 500. The tenant whose ownership disagrees with itself is
  the one an operator most needs to open. ``OwnerRelationshipTests`` proves each
  canonical inconsistency renders as data, at 200.

  IT COULD TIDY UP WHAT IT SAW. A GET that repaired a membership or stamped an
  expired invitation would erase the drift with no actor and no audit row.
  ``ReadsNeverMutateTests`` snapshots the domain across a read.

  IT COULD LEAK A CREDENTIAL. ``NoCredentialExposureTests`` walks the whole rendered
  payload for token material.

WHAT IS NOT RE-ASSERTED HERE. The owner-consistency DEFINITION belongs to
``tests_owner_consistency``; the adoption WRITER's behaviour to
``tests_onboarding_adoption``; the rest of the detail contract to
``tests_restaurant_directory``. This suite owns the projection between them.
"""
import itertools
import json
import uuid
from datetime import timedelta

from django.conf import settings as dj_settings
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from platform_admin_app import onboarding_reads, sessions
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    AdminAuditLog,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import (
    MISSING_OWNER_MEMBERSHIP,
    MULTIPLE_OWNER_MEMBERSHIPS,
    OWNER_MEMBERSHIP_MISMATCH,
)
from platform_admin_app.onboarding_reads import (
    CONTROL_ATTESTED,
    CONTROL_INVITATION_REDEEMED,
    CONTROL_NOT_ESTABLISHED,
    CONTROL_STALE_ATTESTATION,
    EVIDENCE_INVITATION_REDEEMED,
    EVIDENCE_LEGACY_ATTESTATION,
    INVITATION_CANCELLED,
    INVITATION_CONSUMED,
    INVITATION_EXPIRED,
    INVITATION_NOT_APPLICABLE,
    INVITATION_NOT_ISSUED,
    INVITATION_PENDING,
    INVITATION_SUPERSEDED,
    RELATIONSHIP_CONSISTENT,
    STATUS_UNAVAILABLE,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

# Distinct phone range from the other admin suites (…093…). Unbounded: this suite
# builds an owner per restaurant plus replacement owners.
_PHONE = (f'25670930{n:05d}' for n in itertools.count(1))

PASSWORD = 'correct-horse-battery'

_ADMIN_OVERRIDES = dict(
    ROOT_URLCONF='dinify_backend.urls_admin',
    MIDDLEWARE=[
        'platform_admin_app.middleware.RequestIDMiddleware',
        'platform_admin_app.middleware.ClientIPMiddleware',
        *dj_settings.MIDDLEWARE,
    ],
    REST_FRAMEWORK={
        **dj_settings.REST_FRAMEWORK,
        'DEFAULT_AUTHENTICATION_CLASSES': (
            'platform_admin_app.authentication.AdminSessionAuthentication',
        ),
        'DEFAULT_RENDERER_CLASSES': ('rest_framework.renderers.JSONRenderer',),
    },
    ALLOWED_HOSTS=['testserver', 'admin.dinifyapp.com'],
)

LIST_URL = '/admin/v1/restaurants/'


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True, is_active=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=[],
        account_type=account_type, is_active=is_active,
    )


def _make_staff(username='read-admin', email='read-admin@t.com'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def _make_restaurant(name='Read Fixture', *, owner=None, with_owner_membership=True):
    """A structurally consistent restaurant, unless a test asks otherwise."""
    owner = owner or _make_user(f'owner-{uuid.uuid4().hex[:8]}@t.com')
    restaurant = Restaurant.objects.create(
        name=name, location=f'{name} Road', status=RestaurantStatus_Live,
        owner=owner,
    )
    if with_owner_membership:
        RestaurantEmployee.objects.create(
            user=owner, restaurant=restaurant, roles=[RESTAURANT_OWNER],
            active=True, deleted=False,
        )
    return restaurant


def _adopt(restaurant, actor, *, attested_user=None, attested_by=None,
           attested_at=None):
    """
    A ``legacy_adopted`` onboarding row, optionally carrying the attestation triple.

    Built directly rather than through ``adopt_existing_restaurant`` because these are
    READ fixtures: the writer deliberately cannot produce an attestation (that is a
    separate, still-unimplemented decision), so a projection test for the attested
    states has no other way to construct one.
    """
    return RestaurantOnboarding.objects.create(
        restaurant=restaurant,
        source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
        adopted_at=attested_at or timezone.now(),
        adopted_by=actor,
        owner_control_attested_at=attested_at,
        owner_control_attested_user=attested_user,
        owner_control_attested_by=attested_by,
    )


def _admin_create(restaurant, actor):
    """An ``admin_created`` onboarding row — the provenance nothing writes yet."""
    return RestaurantOnboarding.objects.create(
        restaurant=restaurant,
        source=ONBOARDING_SOURCE_ADMIN_CREATED,
        created_by=actor,
    )


def _invite(onboarding, user, issuer, *, consumed=False, cancelled_by=None,
            superseded=False, expires_in=timedelta(days=7), issued_at=None):
    """One ``OwnerInvitation`` in the requested terminal (or live) state."""
    now = timezone.now()
    issued = issued_at or now
    invitation = OwnerInvitation(
        onboarding=onboarding,
        invited_user=user,
        issued_by=issuer,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        issued_at=issued,
        expires_at=issued + expires_in,
    )
    if consumed:
        invitation.consumed_at = now
    elif cancelled_by is not None:
        invitation.cancelled_at = now
        invitation.cancelled_by = cancelled_by
    elif superseded:
        invitation.superseded_at = now
    invitation.save()
    return invitation


def _detail_url(restaurant):
    return f'/admin/v1/restaurants/{restaurant.id}/'


class _ReadTestCase(TestCase):
    """Authenticated (not elevated) admin client, plus terse accessors."""

    def setUp(self):
        super().setUp()
        self.admin = _make_staff()
        raw, self.session = sessions.create_session(self.admin)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

    def detail(self, restaurant):
        response = self.client.get(_detail_url(restaurant))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def onboarding(self, restaurant):
        return self.detail(restaurant)['onboarding']

    def assertControl(self, restaurant, status, evidence=None, evidence_at=None):
        """Assert the whole owner-control verdict — status, evidence and its time."""
        control = self.onboarding(restaurant)['owner_control']
        self.assertEqual(control['status'], status)
        self.assertEqual(control['evidence'], evidence)
        if evidence_at is None:
            self.assertIsNone(control['evidence_at'])
        else:
            self.assertEqual(control['evidence_at'], evidence_at.isoformat())
        return control


@override_settings(**_ADMIN_OVERRIDES)
class UntrackedRestaurantTests(_ReadTestCase):
    """
    A restaurant with no ``RestaurantOnboarding`` row.

    Still the truthful answer for essentially every restaurant: Step 2B shipped the
    adoption mechanism, and adopting any given tenant is a separate explicit decision.
    Every axis reads ``unavailable`` — NOT a negative verdict. An untracked restaurant
    is not "inconsistent" and its owner control is not "not established"; those
    questions have simply not been asked of it, and answering them anyway would
    manufacture a problem out of Step 2 not having reached this tenant.
    """

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant('Untracked Ltd')

    def test_every_axis_reads_unavailable(self):
        block = self.onboarding(self.restaurant)
        self.assertIs(block['tracked'], False)
        self.assertIsNone(block['source'])
        self.assertIsNone(block['recorded_at'])
        self.assertEqual(
            block['owner_relationship'], {'status': STATUS_UNAVAILABLE},
        )
        self.assertEqual(block['invitation'], {
            'status': STATUS_UNAVAILABLE, 'id': None, 'issued_at': None,
            'expires_at': None,
        })
        self.assertEqual(block['owner_control'], {
            'status': STATUS_UNAVAILABLE, 'evidence': None, 'evidence_at': None,
        })

    def test_compatibility_aliases_stay_as_they_were(self):
        owner = self.detail(self.restaurant)['owner']
        self.assertIs(owner['claim_tracked'], False)
        self.assertIsNone(owner['claim_status'])

    def test_an_inconsistent_untracked_restaurant_still_reads_unavailable(self):
        """
        Untracked beats inconsistent. The relationship question is only meaningful
        for a restaurant the onboarding domain has an opinion about.
        """
        drifted = _make_restaurant('Untracked Drift', with_owner_membership=False)
        self.assertEqual(
            self.onboarding(drifted)['owner_relationship']['status'],
            STATUS_UNAVAILABLE,
        )

    def test_the_read_creates_no_onboarding_row(self):
        self.detail(self.restaurant)
        self.assertFalse(RestaurantOnboarding.objects.exists())


@override_settings(**_ADMIN_OVERRIDES)
class LegacyAdoptedTests(_ReadTestCase):
    """
    The provenance that exists in production today, in its three attestation states.
    """

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant('Baba Fixture')
        self.onboarding_row = _adopt(self.restaurant, self.admin)

    def test_the_expected_shape_for_an_adopted_restaurant(self):
        """
        The full contract, which is also what Baba House will read after deploy —
        derived entirely from its data, with no name or UUID special-casing anywhere.
        """
        block = self.onboarding(self.restaurant)
        self.assertEqual(block, {
            'tracked': True,
            'source': ONBOARDING_SOURCE_LEGACY_ADOPTED,
            'recorded_at': self.onboarding_row.adopted_at.isoformat(),
            'owner_relationship': {'status': RELATIONSHIP_CONSISTENT},
            'owner_control': {
                'status': CONTROL_NOT_ESTABLISHED,
                'evidence': None,
                'evidence_at': None,
            },
            # Same KEYS as a represented invitation, all null. A client should not
            # have to branch on the status word to know which keys exist.
            'invitation': {
                'status': INVITATION_NOT_APPLICABLE, 'id': None,
                'issued_at': None, 'expires_at': None,
            },
        })

    def test_recorded_at_is_the_adoption_moment_not_the_restaurant_creation(self):
        """
        ``recorded_at`` answers when the restaurant entered the ADMIN domain. The
        tenant's own creation timestamp is a different field with a different meaning,
        and the detail response already carries it separately.
        """
        block = self.detail(self.restaurant)
        self.assertEqual(
            block['onboarding']['recorded_at'],
            self.onboarding_row.adopted_at.isoformat(),
        )
        self.assertNotEqual(
            block['onboarding']['recorded_at'], block['created_at'],
        )

    def test_compatibility_aliases_track_the_owner_control_status(self):
        owner = self.detail(self.restaurant)['owner']
        self.assertIs(owner['claim_tracked'], True)
        self.assertEqual(owner['claim_status'], CONTROL_NOT_ESTABLISHED)

    def test_an_invitation_is_not_applicable_rather_than_not_issued(self):
        """
        A pre-existing restaurant did not enter Dinify through a claim flow, so no
        invitation is owed. ``not_issued`` would imply one is outstanding.
        """
        self.assertEqual(
            self.onboarding(self.restaurant)['invitation']['status'],
            INVITATION_NOT_APPLICABLE,
        )

    def test_a_valid_attestation_establishes_control(self):
        attested_at = timezone.now() - timedelta(days=3)
        RestaurantOnboarding.objects.filter(pk=self.onboarding_row.pk).update(
            owner_control_attested_at=attested_at,
            owner_control_attested_user=self.restaurant.owner,
            owner_control_attested_by=self.admin,
        )

        self.assertControl(
            self.restaurant, CONTROL_ATTESTED,
            EVIDENCE_LEGACY_ATTESTATION, attested_at,
        )
        owner = self.detail(self.restaurant)['owner']
        self.assertEqual(owner['claim_status'], CONTROL_ATTESTED)

    def test_attestation_means_a_vouched_relationship_not_a_claim_event(self):
        """
        The evidence label says what actually happened: an administrator vouched.
        It must never be reported as an invitation redemption or as the owner having
        claimed the account at some historical moment — neither occurred.
        """
        attested_at = timezone.now()
        RestaurantOnboarding.objects.filter(pk=self.onboarding_row.pk).update(
            owner_control_attested_at=attested_at,
            owner_control_attested_user=self.restaurant.owner,
            owner_control_attested_by=self.admin,
        )
        control = self.onboarding(self.restaurant)['owner_control']
        self.assertEqual(control['evidence'], EVIDENCE_LEGACY_ATTESTATION)
        self.assertNotEqual(control['evidence'], EVIDENCE_INVITATION_REDEEMED)


@override_settings(**_ADMIN_OVERRIDES)
class StaleAttestationTests(_ReadTestCase):
    """
    Evidence naming a previous owner must not count for the current one.

    This is why Step 2A stored the attestation's SUBJECT rather than reading it off
    ``Restaurant.owner``: had the subject been implicit, reassigning the owner would
    silently re-point the evidence and the replacement would inherit control nobody
    vouched for. The projection compares and downgrades; it never rewrites.
    """

    def setUp(self):
        super().setUp()
        self.original_owner = _make_user('original-owner@t.com')
        self.restaurant = _make_restaurant(
            'Handover Ltd', owner=self.original_owner,
        )
        self.attested_at = timezone.now() - timedelta(days=30)
        self.onboarding_row = _adopt(
            self.restaurant, self.admin,
            attested_user=self.original_owner, attested_by=self.admin,
            attested_at=self.attested_at,
        )
        # A real handover: a new owner of record, with the attestation left exactly
        # as it was — which is the state this projection has to interpret.
        self.replacement = _make_user('replacement-owner@t.com')
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            owner=self.replacement,
        )
        self.restaurant.refresh_from_db()

    def test_a_replacement_owner_does_not_inherit_the_attestation(self):
        self.assertControl(
            self.restaurant, CONTROL_STALE_ATTESTATION,
            EVIDENCE_LEGACY_ATTESTATION, self.attested_at,
        )

    def test_stale_is_its_own_state_not_plain_not_established(self):
        """
        The evidence is still surfaced, with its real timestamp, because it really
        happened — an operator needs to see that a PREVIOUS owner was vouched for.
        Collapsing it into ``not_established`` would erase that history.
        """
        control = self.onboarding(self.restaurant)['owner_control']
        self.assertNotEqual(control['status'], CONTROL_NOT_ESTABLISHED)
        self.assertIsNotNone(control['evidence_at'])

    def test_the_attestation_is_not_rewritten_or_cleared_by_the_read(self):
        self.detail(self.restaurant)
        self.onboarding_row.refresh_from_db()
        self.assertEqual(
            self.onboarding_row.owner_control_attested_user_id,
            self.original_owner.id,
        )
        self.assertEqual(
            self.onboarding_row.owner_control_attested_at, self.attested_at,
        )

    def test_the_compatibility_alias_reports_stale_too(self):
        """The frontend's old field must not read as a clean claim."""
        owner = self.detail(self.restaurant)['owner']
        self.assertEqual(owner['claim_status'], CONTROL_STALE_ATTESTATION)


@override_settings(**_ADMIN_OVERRIDES)
class OwnerRelationshipTests(_ReadTestCase):
    """
    Each canonical inconsistency renders as DATA, at 200.

    A drifted tenant is the one an operator most needs to open. A 500 on its detail
    page would hide the problem behind the symptom, and the codes are surfaced raw so
    what the screen says is greppable in the audit log and the codebase.
    """

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant(
            'Drifted Ltd', with_owner_membership=False,
        )
        _adopt(self.restaurant, self.admin)

    def relationship(self):
        return self.onboarding(self.restaurant)['owner_relationship']['status']

    def test_consistent(self):
        RestaurantEmployee.objects.create(
            user=self.restaurant.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.assertEqual(self.relationship(), RELATIONSHIP_CONSISTENT)

    def test_missing_owner_membership(self):
        self.assertEqual(self.relationship(), MISSING_OWNER_MEMBERSHIP)

    def test_owner_membership_mismatch(self):
        RestaurantEmployee.objects.create(
            user=_make_user('stranger@t.com'), restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.assertEqual(self.relationship(), OWNER_MEMBERSHIP_MISMATCH)

    def test_multiple_owner_memberships(self):
        RestaurantEmployee.objects.create(
            user=self.restaurant.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        RestaurantEmployee.objects.create(
            user=_make_user('co-owner@t.com'), restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.assertEqual(self.relationship(), MULTIPLE_OWNER_MEMBERSHIPS)

    def test_a_manager_does_not_satisfy_the_relationship(self):
        RestaurantEmployee.objects.create(
            user=self.restaurant.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_MANAGER],
        )
        self.assertEqual(self.relationship(), MISSING_OWNER_MEMBERSHIP)

    def test_relationship_and_control_are_independent_axes(self):
        """
        A drifted relationship does not by itself say anything about owner control,
        and vice versa. Two questions, two answers.
        """
        block = self.onboarding(self.restaurant)
        self.assertEqual(
            block['owner_relationship']['status'], MISSING_OWNER_MEMBERSHIP,
        )
        self.assertEqual(
            block['owner_control']['status'], CONTROL_NOT_ESTABLISHED,
        )

    def test_no_membership_identifiers_leak_into_the_relationship_block(self):
        """
        The status word is the whole payload. ``OwnerConsistencyError.details`` carries
        membership user ids that add nothing actionable to this screen, and the
        narrower the object the fewer ways it can grow a leak.
        """
        RestaurantEmployee.objects.create(
            user=_make_user('leak-check@t.com'), restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.assertEqual(
            set(self.onboarding(self.restaurant)['owner_relationship']), {'status'},
        )


@override_settings(**_ADMIN_OVERRIDES)
class AdminCreatedInvitationTests(_ReadTestCase):
    """
    ``admin_created`` provenance: control comes from a consumed invitation, or from
    nothing at all.

    Nothing writes this provenance yet — the fixtures build it directly — but the
    projection has to be right BEFORE the invitation service exists, or the service
    will be written against a read that was never exercised.
    """

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant('Admin Made Ltd')
        self.onboarding_row = _admin_create(self.restaurant, self.admin)

    def invitation_status(self):
        return self.onboarding(self.restaurant)['invitation']['status']

    def test_recorded_at_is_the_onboarding_creation(self):
        self.assertEqual(
            self.onboarding(self.restaurant)['recorded_at'],
            self.onboarding_row.created_at.isoformat(),
        )

    def test_no_invitation_at_all(self):
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)
        self.assertEqual(self.invitation_status(), INVITATION_NOT_ISSUED)

    def test_a_pending_invitation_establishes_nothing(self):
        _invite(self.onboarding_row, self.restaurant.owner, self.admin)
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)
        self.assertEqual(self.invitation_status(), INVITATION_PENDING)

    def test_an_expired_unresolved_invitation_establishes_nothing(self):
        """
        Expiry is DERIVED against the clock, never stored: nothing in this repository
        runs on a schedule to maintain a status column, and an expired invitation is
        still unresolved — it holds the per-onboarding slot until something supersedes
        it.
        """
        _invite(
            self.onboarding_row, self.restaurant.owner, self.admin,
            issued_at=timezone.now() - timedelta(days=30),
            expires_in=timedelta(days=7),
        )
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)
        self.assertEqual(self.invitation_status(), INVITATION_EXPIRED)

    def test_a_consumed_invitation_for_the_current_owner_establishes_control(self):
        invitation = _invite(
            self.onboarding_row, self.restaurant.owner, self.admin, consumed=True,
        )
        self.assertControl(
            self.restaurant, CONTROL_INVITATION_REDEEMED,
            EVIDENCE_INVITATION_REDEEMED, invitation.consumed_at,
        )
        self.assertEqual(self.invitation_status(), INVITATION_CONSUMED)
        self.assertEqual(
            self.detail(self.restaurant)['owner']['claim_status'],
            CONTROL_INVITATION_REDEEMED,
        )

    def test_a_consumed_invitation_for_a_different_user_establishes_nothing(self):
        """
        The invitation half of the stale-evidence rule. A credential consumed by
        somebody who is not the owner proves that person's control, and nobody else's
        — a replacement owner must not inherit it.
        """
        previous = _make_user('previous-owner@t.com')
        _invite(self.onboarding_row, previous, self.admin, consumed=True)

        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)

    def test_a_consumed_invitation_for_a_previous_owner_still_reports_its_history(self):
        """
        ``invitation: consumed`` alongside ``owner_control: not_established`` is NOT a
        contradiction — it is the honest reading of "somebody consumed an invitation
        and is no longer the owner". The invitation axis reports what happened to the
        credential; the control axis reports whether the CURRENT owner is covered.
        """
        previous = _make_user('handover-previous@t.com')
        _invite(self.onboarding_row, previous, self.admin, consumed=True)

        block = self.onboarding(self.restaurant)
        self.assertEqual(block['invitation']['status'], INVITATION_CONSUMED)
        self.assertEqual(
            block['owner_control']['status'], CONTROL_NOT_ESTABLISHED,
        )

    def test_a_cancelled_invitation_establishes_nothing(self):
        _invite(
            self.onboarding_row, self.restaurant.owner, self.admin,
            cancelled_by=self.admin,
        )
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)
        self.assertEqual(self.invitation_status(), INVITATION_CANCELLED)

    def test_a_superseded_invitation_establishes_nothing(self):
        _invite(
            self.onboarding_row, self.restaurant.owner, self.admin, superseded=True,
        )
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)
        self.assertEqual(self.invitation_status(), INVITATION_SUPERSEDED)

    def test_a_live_invitation_outranks_resolved_history(self):
        """
        A reissue: the old attempt was superseded and a fresh one is outstanding. The
        operator needs to see the LIVE one — the resolved row is history.
        """
        _invite(
            self.onboarding_row, self.restaurant.owner, self.admin, superseded=True,
            issued_at=timezone.now() - timedelta(days=2),
        )
        _invite(self.onboarding_row, self.restaurant.owner, self.admin)
        self.assertEqual(self.invitation_status(), INVITATION_PENDING)

    def test_current_owner_redemption_outranks_a_later_live_invitation(self):
        """
        Control, once established for the current owner, is not undone by a new
        invitation being outstanding — the owner already proved control.
        """
        consumed = _invite(
            self.onboarding_row, self.restaurant.owner, self.admin, consumed=True,
            issued_at=timezone.now() - timedelta(days=5),
        )
        _invite(self.onboarding_row, _make_user('someone-else@t.com'), self.admin)

        self.assertControl(
            self.restaurant, CONTROL_INVITATION_REDEEMED,
            EVIDENCE_INVITATION_REDEEMED, consumed.consumed_at,
        )
        self.assertEqual(self.invitation_status(), INVITATION_CONSUMED)

    def test_the_most_recent_current_owner_redemption_is_used(self):
        """
        The database does not forbid two consumed rows for one user, so the
        projection picks the latest rather than pretending it cannot happen.
        """
        old = timezone.now() - timedelta(days=10)
        _invite(
            self.onboarding_row, self.restaurant.owner, self.admin, superseded=True,
            issued_at=old,
        )
        OwnerInvitation.objects.filter(pk=OwnerInvitation.objects.latest(
            'issued_at').pk).update(superseded_at=None, consumed_at=old)

        recent = _invite(
            self.onboarding_row, self.restaurant.owner, self.admin, consumed=True,
        )
        self.assertControl(
            self.restaurant, CONTROL_INVITATION_REDEEMED,
            EVIDENCE_INVITATION_REDEEMED, recent.consumed_at,
        )


@override_settings(**_ADMIN_OVERRIDES)
class ControlIsNeverInferredTests(_ReadTestCase):
    """
    THE CENTRAL GUARANTEE: nothing except the two named kinds of evidence establishes
    owner control.

    Each signal below is easy to reach for, plausible-looking, and answers a different
    question — an account exists and has been used, not that the right human is behind
    it. A projection that guessed would be indistinguishable from one that knew, which
    is the failure the whole domain design exists to prevent.
    """

    def setUp(self):
        super().setUp()
        self.owner = _make_user('busy-owner@t.com')
        self.restaurant = _make_restaurant('Inference Ltd', owner=self.owner)
        _adopt(self.restaurant, self.admin)

    def test_an_active_recently_used_owner_account_establishes_nothing(self):
        User.objects.filter(pk=self.owner.pk).update(
            is_active=True, last_login=timezone.now(),
        )
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)

    def test_a_settled_password_state_establishes_nothing(self):
        if hasattr(self.owner, 'prompt_password_change'):
            User.objects.filter(pk=self.owner.pk).update(
                prompt_password_change=False,
            )
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)

    def test_owner_membership_and_a_consistent_relationship_establish_nothing(self):
        """
        The sharpest case: this restaurant is perfectly consistent — owner FK and
        active owner membership agree — and control is still not established. Agreeing
        about WHO the owner is says nothing about whether that person controls it.
        """
        block = self.onboarding(self.restaurant)
        self.assertEqual(
            block['owner_relationship']['status'], RELATIONSHIP_CONSISTENT,
        )
        self.assertEqual(
            block['owner_control']['status'], CONTROL_NOT_ESTABLISHED,
        )

    def test_a_deactivated_owner_does_not_change_the_control_verdict_either(self):
        """
        Account eligibility is a separate axis, as it is for the writer. Deactivation
        is not evidence AGAINST control any more than activity is evidence for it.
        """
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertControl(self.restaurant, CONTROL_NOT_ESTABLISHED)
        self.assertEqual(
            self.onboarding(self.restaurant)['owner_relationship']['status'],
            RELATIONSHIP_CONSISTENT,
        )


@override_settings(**_ADMIN_OVERRIDES)
class ReadsNeverMutateTests(_ReadTestCase):
    """
    A GET changes nothing and records nothing.

    Repairing on the way past would erase the drift with no actor, no reason and no
    audit row — and would make it undetectable next time. Auditing the read would bury
    real decisions under directory page-views; ``AdminAuditLog`` is a record of
    privileged DECISIONS, not an access log.
    """

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant(
            'Untouched Ltd', with_owner_membership=False,
        )
        self.onboarding_row = _adopt(self.restaurant, self.admin)
        self.stranger = _make_user('untouched-stranger@t.com')
        self.membership = RestaurantEmployee.objects.create(
            user=self.stranger, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.invitation_count = OwnerInvitation.objects.count()

    def test_an_inconsistent_relationship_is_reported_not_repaired(self):
        self.assertEqual(
            self.onboarding(self.restaurant)['owner_relationship']['status'],
            OWNER_MEMBERSHIP_MISMATCH,
        )

        self.membership.refresh_from_db()
        self.restaurant.refresh_from_db()
        self.assertTrue(self.membership.active)
        self.assertEqual(self.membership.roles, [RESTAURANT_OWNER])
        self.assertNotEqual(self.restaurant.owner_id, self.stranger.id)
        self.assertEqual(
            RestaurantEmployee.objects.filter(restaurant=self.restaurant).count(), 1,
        )

    def test_the_onboarding_row_is_unchanged(self):
        fields = (
            'source', 'adopted_at', 'adopted_by_id', 'created_by_id',
            'owner_control_attested_at', 'owner_control_attested_user_id',
            'owner_control_attested_by_id', 'updated_at',
        )
        before = {f: getattr(self.onboarding_row, f) for f in fields}

        self.detail(self.restaurant)

        self.onboarding_row.refresh_from_db()
        after = {f: getattr(self.onboarding_row, f) for f in fields}
        self.assertEqual(after, before)

    def test_an_expired_invitation_is_not_stamped_by_reading_it(self):
        onboarding = _admin_create(_make_restaurant('Expiry Ltd'), self.admin)
        invitation = _invite(
            onboarding, onboarding.restaurant.owner, self.admin,
            issued_at=timezone.now() - timedelta(days=30),
            expires_in=timedelta(days=1),
        )

        self.assertEqual(
            self.onboarding(onboarding.restaurant)['invitation']['status'],
            INVITATION_EXPIRED,
        )

        invitation.refresh_from_db()
        self.assertIsNone(invitation.consumed_at)
        self.assertIsNone(invitation.cancelled_at)
        self.assertIsNone(invitation.superseded_at)

    def test_the_read_writes_no_audit_row(self):
        before = AdminAuditLog.objects.count()
        self.detail(self.restaurant)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_no_invitation_rows_are_created(self):
        self.detail(self.restaurant)
        self.assertEqual(OwnerInvitation.objects.count(), self.invitation_count)


@override_settings(**_ADMIN_OVERRIDES)
class NoCredentialExposureTests(_ReadTestCase):
    """
    An invitation is projected as a STATE WORD and, where it is evidence, a timestamp.

    Never the token hash, never a raw token, never a claim URL. The whole rendered
    payload is searched rather than the invitation block alone, because the leak worth
    catching is the one that appears somewhere nobody thought to look.
    """

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant('Sealed Ltd')
        self.onboarding_row = _admin_create(self.restaurant, self.admin)
        self.invitation = _invite(
            self.onboarding_row, self.restaurant.owner, self.admin, consumed=True,
        )

    def test_the_token_hash_appears_nowhere_in_the_response(self):
        body = json.dumps(self.detail(self.restaurant))
        self.assertNotIn(self.invitation.token_hash, body)

    def test_no_credential_shaped_keys_appear_anywhere(self):
        body = json.dumps(self.detail(self.restaurant)).lower()
        for term in (
            'token', 'claim_url', 'claim_link', 'password', 'totp', 'secret',
            'recovery', 'credential', 'invite_url',
        ):
            self.assertNotIn(term, body, f'{term!r} leaked into the detail payload')

    def test_the_invitation_block_carries_a_status_and_safe_metadata_only(self):
        """
        Step 2E added ``id``/``issued_at``/``expires_at`` so an operator can name the
        exact invitation they reviewed when they reissue or cancel it. The block is
        asserted EXACTLY, so a fifth key cannot appear without this failing — which is
        the whole guard: an id is an opaque handle, a token is a credential, and the
        two must not become interchangeable because they sit in the same object.
        """
        block = self.onboarding(self.restaurant)['invitation']
        self.assertEqual(
            set(block), {'status', 'id', 'issued_at', 'expires_at'},
        )
        self.assertEqual(block['status'], INVITATION_CONSUMED)
        self.assertEqual(block['id'], str(self.invitation.id))
        self.assertEqual(block['issued_at'], self.invitation.issued_at.isoformat())
        self.assertEqual(block['expires_at'], self.invitation.expires_at.isoformat())

    def test_the_projected_id_is_the_row_id_and_not_the_token_hash(self):
        """
        Stated separately because "we expose an identifier" and "we expose the RIGHT
        identifier" are different claims, and the second is the one that matters.
        """
        block = self.onboarding(self.restaurant)['invitation']
        self.assertNotEqual(block['id'], self.invitation.token_hash)
        self.assertNotIn(self.invitation.token_hash, json.dumps(block))

    def test_owner_pii_is_not_expanded_beyond_the_approved_fields(self):
        """
        The owner block keeps exactly the Step 1 contract. The onboarding object adds
        no second copy of the owner's identity and no new contact detail.
        """
        data = self.detail(self.restaurant)
        self.assertEqual(
            set(data['owner']),
            {'id', 'name', 'email', 'phone_number', 'is_active',
             'claim_tracked', 'claim_status'},
        )
        onboarding_body = json.dumps(data['onboarding'])
        self.assertNotIn(self.restaurant.owner.email, onboarding_body)
        self.assertNotIn(str(self.restaurant.owner.phone_number), onboarding_body)


@override_settings(**_ADMIN_OVERRIDES)
class DirectoryIsUnchangedTests(_ReadTestCase):
    """
    Step 2C is DETAIL-ONLY. The directory row must not grow the field, and must not
    start paying for it.

    The onboarding record belongs in the restaurant workspace; adding it to a list row
    would mean per-row joins on every page of a portfolio before any screen has asked
    for the data.
    """

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant('Listed Ltd')
        _adopt(self.restaurant, self.admin)

    def _row(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.content)
        return next(
            r for r in response.json()['data']['results']
            if r['id'] == str(self.restaurant.id)
        )

    def test_the_directory_row_carries_no_onboarding_field(self):
        row = self._row()
        self.assertNotIn('onboarding', row)
        self.assertNotIn('owner', row)

    def test_the_directory_query_count_does_not_grow_with_adopted_restaurants(self):
        """
        The list must cost the same whether or not its restaurants are tracked — if
        the projection had leaked into ``serialize_row`` this is what would catch it.
        """
        def measure():
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(LIST_URL, {'page_size': 20})
                self.assertEqual(response.status_code, 200, response.content)
            return len(captured)

        untracked_only = measure()

        for index in range(8):
            adopted = _make_restaurant(f'Listed Adopted {index}')
            _adopt(adopted, self.admin)

        self.assertEqual(
            untracked_only, measure(),
            'The directory started paying for onboarding rows — Step 2C is '
            'detail-only.',
        )


@override_settings(**_ADMIN_OVERRIDES)
class DetailQueryCountTests(_ReadTestCase):
    """
    The projection's cost is bounded and constant in the size of the tenant's history.

    The absolute numbers are not pinned — they legitimately include session/auth reads
    that are not this endpoint's concern. What is asserted is that nothing here grows
    per row, which is the failure mode that would matter.
    """

    def _measure(self, restaurant):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(_detail_url(restaurant))
            self.assertEqual(response.status_code, 200, response.content)
        return len(captured)

    def test_invitation_history_does_not_add_queries(self):
        restaurant = _make_restaurant('Costly Ltd')
        onboarding = _admin_create(restaurant, self.admin)
        lean = self._measure(restaurant)

        for index in range(10):
            _invite(
                onboarding, _make_user(f'invitee-{index}@t.com'), self.admin,
                superseded=True,
                issued_at=timezone.now() - timedelta(days=index + 1),
            )

        self.assertEqual(
            lean, self._measure(restaurant),
            'Invitation history added queries — the projection is fetching rows '
            'per invitation instead of asking bounded questions.',
        )

    def test_employee_count_does_not_add_queries(self):
        restaurant = _make_restaurant('Staffed Ltd')
        _adopt(restaurant, self.admin)
        lean = self._measure(restaurant)

        for index in range(10):
            RestaurantEmployee.objects.create(
                user=_make_user(f'staff-{index}@t.com'), restaurant=restaurant,
                roles=[RESTAURANT_MANAGER],
            )

        self.assertEqual(lean, self._measure(restaurant))

    def test_an_untracked_restaurant_costs_one_extra_query(self):
        """
        The onboarding lookup that decides ``tracked: false`` is the whole cost for the
        common case — no consistency read, no invitation read, because neither question
        applies to a restaurant the domain has no opinion about.
        """
        untracked = _make_restaurant('Cheap Ltd')
        tracked = _make_restaurant('Tracked Ltd')
        _adopt(tracked, self.admin)

        self.assertEqual(
            self._measure(tracked) - self._measure(untracked), 1,
            'A tracked restaurant should cost exactly one more query than an '
            'untracked one (the owner-consistency read).',
        )


class ProjectionIsPureTests(TestCase):
    """
    The helper itself, called directly — no HTTP, no session, no client.

    ``onboarding_summary`` is the seam a future Admin endpoint or report will reuse,
    so its read-only-ness is asserted against the function rather than only through
    the view that happens to call it today.
    """

    def setUp(self):
        super().setUp()
        self.admin = _make_staff(username='pure-admin', email='pure@t.com')
        self.restaurant = _make_restaurant('Pure Ltd')

    def test_it_opens_no_transaction_and_issues_only_selects(self):
        _adopt(self.restaurant, self.admin)
        with CaptureQueriesContext(connection) as captured:
            onboarding_reads.onboarding_summary(self.restaurant)

        for query in captured.captured_queries:
            sql = query['sql'].strip().lower()
            self.assertTrue(
                sql.startswith('select'),
                f'A read projection issued a non-SELECT statement: {sql[:120]}',
            )

    def test_it_returns_the_untracked_shape_without_creating_anything(self):
        summary = onboarding_reads.onboarding_summary(self.restaurant)
        self.assertIs(summary['tracked'], False)
        self.assertFalse(RestaurantOnboarding.objects.exists())

    def test_serialize_owner_defaults_to_the_untracked_projection(self):
        """
        Called without a summary — the signature's default — it answers exactly what
        it always did, so no caller can accidentally get a claim verdict it never
        asked the domain for.
        """
        from platform_admin_app.restaurant_reads import serialize_owner

        owner = serialize_owner(self.restaurant.owner)
        self.assertIs(owner['claim_tracked'], False)
        self.assertIsNone(owner['claim_status'])
