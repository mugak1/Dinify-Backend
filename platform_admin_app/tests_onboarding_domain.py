"""
The onboarding domain's persisted invariants: ``RestaurantOnboarding`` +
``OwnerInvitation``.

These tests are deliberately about the DATABASE, not about Django. Nothing here
restates that a ``CharField`` stores a string or that a ``OneToOneField`` is unique
in the abstract — every case below pins a Dinify decision that a later service
could otherwise quietly violate:

  PROVENANCE CANNOT BE HALF-TOLD. A row that cannot say how a restaurant arrived,
  or that claims to be admin-created while carrying an adoption timestamp, is worse
  than no row: it is a confident answer to a question nobody actually answered. The
  vocabulary and both shapes are CHECK constraints, so a buggy future writer fails
  loudly at the integrity boundary rather than persisting a plausible fiction.

  ATTESTATION IS NOT A HISTORICAL CLAIM, AND IT NAMES ITS SUBJECT. The triple —
  when, by whom, about whom — moves together, and only legacy provenance may carry
  it. A partial triple is either an unattributable assertion, an assertion about no
  particular moment, or (worst) an assertion about no particular PERSON, which
  would silently re-point at whoever `Restaurant.owner` becomes next.

  A CREDENTIAL RESOLVES ONCE. An invitation that is both consumed and cancelled
  cannot be reported honestly, and two live invitations for one onboarding means
  two people were asked to become the same owner.

  EXPIRY IS DERIVED, NOT STAMPED. The expired-still-occupies-the-slot case
  (`ExpiredInvitationStillHoldsTheSlotTests`) is the one that looks like a bug and
  is not: a partial-index predicate must be immutable, so the unresolved-slot index
  cannot consult the clock. That is precisely what makes the future reissue path's
  supersede step load-bearing.

WHAT IS NOT ASSERTED HERE: that anything CREATES these rows. Nothing does yet, and
`NoAutomaticOnboardingTests` pins that absence, because the failure mode of a
signal or a backfill is silent — every restaurant suddenly rendering an onboarding
state nobody decided.
"""
from contextlib import contextmanager
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RestaurantStatus_Live,
)
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    OwnerInvitation,
    RestaurantOnboarding,
)
from restaurants_app.models import Restaurant
from users_app.models import User

# Distinct phone range from the other admin suites (…081…) so the unique
# phone_number constraint cannot collide when suites run in one process.
_PHONE = iter(f'2567081000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=[],
        account_type=account_type,
    )


def _make_staff(username, email=None):
    return _make_user(
        email or f'{username}@t.com',
        account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        username=username,
        phone=False,
    )


def _make_restaurant(name):
    return Restaurant.objects.create(
        name=name, location=f'{name} Road', status=RestaurantStatus_Live,
        owner=_make_user(f'owner-{name}@t.com'.replace(' ', '-')),
    )


def _concrete_field_names(model):
    """Column-backed field names only — reverse accessors are not storage."""
    return {field.name for field in model._meta.fields}


def _column_names(model):
    """The actual database columns, so an FK shows up as ``<name>_id``."""
    return {field.column for field in model._meta.fields}


class _IntegrityAssertions:
    """Shared assertion for "the database refused this, for THIS reason"."""

    @contextmanager
    def assertViolates(self, constraint=None):
        """
        Assert the wrapped write raises ``IntegrityError``, naming ``constraint``.

        The inner ``atomic()`` is required, not decorative: an IntegrityError
        poisons the surrounding test transaction, so without a savepoint every
        assertion after this one would fail on a broken connection.

        The constraint NAME is only asserted on PostgreSQL. Postgres names the
        violated constraint in every integrity error — check and unique alike — and
        that is the backend CI and production run, so this is where knowing WHICH
        rule fired matters. SQLite does not name a violated unique index, and a test
        that pinned the rule on one backend and not the other would be a portability
        trap dressed as a stronger assertion.
        """
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                yield
        if constraint is not None and connection.vendor == 'postgresql':
            self.assertIn(
                constraint, str(caught.exception),
                msg=(
                    'The write was refused, but by a different constraint than '
                    f'{constraint!r} — the invariant under test may not be the one '
                    'that fired.'
                ),
            )


class _OnboardingFixture(_IntegrityAssertions, TestCase):
    """One restaurant and the three kinds of platform actor a row can name."""

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant('Provenance House')
        self.creator = _make_staff('onboarding-creator')
        self.adopter = _make_staff('onboarding-adopter')
        self.attestor = _make_staff('onboarding-attestor')

    def admin_created(self, **overrides):
        """A well-formed admin-created row, with fields overridable per case."""
        payload = dict(
            restaurant=self.restaurant,
            source=ONBOARDING_SOURCE_ADMIN_CREATED,
            created_by=self.creator,
        )
        payload.update(overrides)
        return RestaurantOnboarding.objects.create(**payload)

    def legacy_adopted(self, **overrides):
        """A well-formed legacy-adopted row, with fields overridable per case."""
        payload = dict(
            restaurant=self.restaurant,
            source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(),
            adopted_by=self.adopter,
        )
        payload.update(overrides)
        return RestaurantOnboarding.objects.create(**payload)

    def attestation(self, **overrides):
        """A COMPLETE attestation triple, so a case must opt in to breaking it."""
        payload = dict(
            owner_control_attested_at=timezone.now(),
            owner_control_attested_user=self.restaurant.owner,
            owner_control_attested_by=self.attestor,
        )
        payload.update(overrides)
        return payload


# --- A: admin-created provenance -----------------------------------------------------

class AdminCreatedProvenanceTests(_OnboardingFixture):
    def test_admin_created_row_names_its_creator_and_nothing_else(self):
        row = self.admin_created()

        self.assertEqual(row.source, ONBOARDING_SOURCE_ADMIN_CREATED)
        self.assertEqual(row.created_by_id, self.creator.pk)
        self.assertIsNone(row.adopted_at)
        self.assertIsNone(row.adopted_by_id)
        self.assertIsNone(row.owner_control_attested_at)
        self.assertIsNone(row.owner_control_attested_user_id)
        self.assertIsNone(row.owner_control_attested_by_id)

    def test_admin_created_without_a_creator_is_refused(self):
        # Dinify pressed the button; an admin-created restaurant with nobody behind
        # its creation is an unattributable act, not a provenance record.
        with self.assertViolates('restaurant_onboarding_admin_created_shape'):
            self.admin_created(created_by=None)

    def test_admin_created_cannot_carry_an_adoption_timestamp(self):
        with self.assertViolates('restaurant_onboarding_admin_created_shape'):
            self.admin_created(adopted_at=timezone.now())

    def test_admin_created_cannot_carry_an_adopter(self):
        with self.assertViolates('restaurant_onboarding_admin_created_shape'):
            self.admin_created(adopted_by=self.adopter)


# --- B + C + D: legacy-adopted provenance --------------------------------------------

class LegacyAdoptedProvenanceTests(_OnboardingFixture):
    def test_legacy_row_records_who_reconciled_it_and_when(self):
        before = timezone.now()
        row = self.legacy_adopted()

        self.assertEqual(row.source, ONBOARDING_SOURCE_LEGACY_ADOPTED)
        self.assertEqual(row.adopted_by_id, self.adopter.pk)
        self.assertGreaterEqual(row.adopted_at, before)
        self.assertIsNone(row.created_by_id)

    def test_legacy_without_an_adoption_timestamp_is_refused(self):
        with self.assertViolates('restaurant_onboarding_legacy_adopted_shape'):
            self.legacy_adopted(adopted_at=None)

    def test_legacy_without_an_adopter_is_refused(self):
        with self.assertViolates('restaurant_onboarding_legacy_adopted_shape'):
            self.legacy_adopted(adopted_by=None)

    def test_legacy_cannot_claim_dinify_created_it(self):
        # `created_by` means "Dinify created this restaurant". A legacy tenant
        # pre-dates Admin entirely, so the field has no honest value here.
        with self.assertViolates('restaurant_onboarding_legacy_adopted_shape'):
            self.legacy_adopted(created_by=self.creator)

    def test_legacy_without_owner_control_attestation_is_a_valid_row(self):
        # THE HONEST STATE, and the one Baba House will land in first: the ownership
        # relationship exists technically, and nobody has yet vouched for it. This
        # must be representable without inventing a claim that never happened.
        row = self.legacy_adopted()

        self.assertIsNone(row.owner_control_attested_at)
        self.assertIsNone(row.owner_control_attested_user_id)
        self.assertIsNone(row.owner_control_attested_by_id)

    def test_legacy_with_owner_control_attestation_is_a_valid_row(self):
        attested_at = timezone.now()
        row = self.legacy_adopted(
            **self.attestation(owner_control_attested_at=attested_at)
        )

        # The whole sentence is stored: at THIS time, THIS administrator attested
        # that THIS user controls the restaurant. Never "the owner claimed the
        # account at this historical timestamp", and never a subject left implicit.
        self.assertEqual(row.owner_control_attested_at, attested_at)
        self.assertEqual(row.owner_control_attested_by_id, self.attestor.pk)
        self.assertEqual(
            row.owner_control_attested_user_id, self.restaurant.owner_id,
        )


# --- E + F: the attestation triple --------------------------------------------------

class OwnerControlAttestationTripleTests(_OnboardingFixture):
    """
    An attestation is one sentence — WHEN, BY WHOM, ABOUT WHOM — and a partial
    triple is a sentence missing a word. Each omission below fails differently, and
    the missing SUBJECT is the dangerous one: without it the evidence would attach
    to whatever ``Restaurant.owner`` points at next.
    """

    def test_attestation_timestamp_alone_is_refused(self):
        with self.assertViolates('restaurant_onboarding_attestation_triple'):
            self.legacy_adopted(owner_control_attested_at=timezone.now())

    def test_attestor_alone_is_refused(self):
        with self.assertViolates('restaurant_onboarding_attestation_triple'):
            self.legacy_adopted(owner_control_attested_by=self.attestor)

    def test_attested_subject_alone_is_refused(self):
        with self.assertViolates('restaurant_onboarding_attestation_triple'):
            self.legacy_adopted(owner_control_attested_user=self.restaurant.owner)

    def test_attestation_without_a_subject_is_refused(self):
        # THE ONE THAT MATTERS. A timestamp and an attestor with no named subject
        # would certify "the current owner, whoever that turns out to be" — so
        # reassigning the owner would hand the replacement control evidence nobody
        # ever gave them, and no reader could tell the difference.
        with self.assertViolates('restaurant_onboarding_attestation_triple'):
            self.legacy_adopted(
                **self.attestation(owner_control_attested_user=None)
            )

    def test_attestation_without_an_attestor_is_refused(self):
        with self.assertViolates('restaurant_onboarding_attestation_triple'):
            self.legacy_adopted(**self.attestation(owner_control_attested_by=None))

    def test_attestation_without_a_timestamp_is_refused(self):
        with self.assertViolates('restaurant_onboarding_attestation_triple'):
            self.legacy_adopted(**self.attestation(owner_control_attested_at=None))

    def test_attestation_cannot_attach_to_admin_created_provenance(self):
        # Control over a restaurant Dinify created is established by an invitation
        # that was actually consumed. Attestation exists to represent legacy data
        # honestly, not to shortcut a claim the platform could have observed.
        with self.assertViolates('restaurant_onboarding_admin_created_shape'):
            self.admin_created(**self.attestation())

    def test_the_subject_alone_cannot_be_smuggled_onto_admin_created(self):
        with self.assertViolates('restaurant_onboarding_admin_created_shape'):
            self.admin_created(owner_control_attested_user=self.restaurant.owner)


class AttestationIsBoundToTheOwnerItCertifiesTests(_OnboardingFixture):
    """
    The attestation names its subject, so it cannot silently transfer.

    `Restaurant.owner` has no write path today — it is absent from
    `EDIT_INFORMATION['restaurants']` and `read_only` on `SerializerPutRestaurant`
    — but owner reassignment is a Step 2/3 feature, and this schema is being frozen
    now. These tests pin the property that makes a later reassignment safe WITHOUT
    every future owner-write path having to remember to clear anything: the stored
    subject and the current FK simply stop matching, and a reader can see it.
    """

    def test_the_attested_subject_does_not_follow_a_reassigned_owner(self):
        row = self.legacy_adopted(**self.attestation())
        original_owner_id = self.restaurant.owner_id

        replacement = _make_user('replacement-owner@t.com')
        Restaurant.objects.filter(pk=self.restaurant.pk).update(owner=replacement)

        row.refresh_from_db()
        self.restaurant.refresh_from_db()

        # The evidence still names the person it was actually given about...
        self.assertEqual(row.owner_control_attested_user_id, original_owner_id)
        # ...so it no longer matches the current owner of record, which is exactly
        # the comparison a claim-state reader makes. Had the subject been implicit,
        # the replacement would have inherited this attestation in silence.
        self.assertEqual(self.restaurant.owner_id, replacement.pk)
        self.assertNotEqual(
            row.owner_control_attested_user_id, self.restaurant.owner_id,
        )

    def test_a_current_attestation_matches_the_owner_of_record(self):
        row = self.legacy_adopted(**self.attestation())

        self.restaurant.refresh_from_db()
        self.assertEqual(
            row.owner_control_attested_user_id, self.restaurant.owner_id,
        )

    def test_the_subject_need_not_equal_the_attestor(self):
        # Two different people in two different roles: the administrator who
        # vouched, and the owner vouched for. Nothing conflates them.
        row = self.legacy_adopted(**self.attestation())

        self.assertNotEqual(
            row.owner_control_attested_user_id, row.owner_control_attested_by_id,
        )

    def test_deleting_the_attested_owner_is_refused(self):
        subject = _make_user('attested-subject@t.com')
        self.legacy_adopted(
            **self.attestation(owner_control_attested_user=subject)
        )

        # The subject IS the evidence — an attestation about a user who no longer
        # exists certifies nobody.
        with self.assertRaises(ProtectedError):
            subject.delete()


# --- G: one onboarding per restaurant ------------------------------------------------

class OneOnboardingPerRestaurantTests(_OnboardingFixture):
    def test_a_restaurant_cannot_enter_the_domain_twice(self):
        self.legacy_adopted()

        # Two provenance rows for one tenant would mean two answers to "how did this
        # arrive", and the reverse accessor could only show one of them.
        with self.assertViolates():
            self.admin_created()

        self.assertEqual(RestaurantOnboarding.objects.count(), 1)

    def test_the_reverse_accessor_reaches_the_single_row(self):
        row = self.legacy_adopted()

        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.admin_onboarding.pk, row.pk)


# --- H: the source vocabulary is a database boundary ---------------------------------

class SourceVocabularyTests(_OnboardingFixture):
    def test_an_unknown_source_is_refused_by_the_database(self):
        # `.objects.create()` deliberately, NOT `full_clean()`: `choices=` is a form
        # and admin nicety that a service calling the ORM directly never consults.
        # The vocabulary has to hold at the integrity boundary or it holds nowhere.
        with self.assertViolates('restaurant_onboarding_source_vocabulary'):
            RestaurantOnboarding.objects.create(
                restaurant=self.restaurant,
                source='self_signup',
                created_by=self.creator,
            )

    def test_omitting_the_source_is_refused_rather_than_defaulted(self):
        # There is no semantic default. A caller that forgets to say how the
        # restaurant arrived inserts the empty string, and the empty string is not
        # in the vocabulary — so it fails instead of silently becoming a provenance.
        with self.assertViolates('restaurant_onboarding_source_vocabulary'):
            RestaurantOnboarding.objects.create(
                restaurant=self.restaurant,
                created_by=self.creator,
            )

    def test_the_field_declares_no_default(self):
        field = RestaurantOnboarding._meta.get_field('source')

        self.assertFalse(
            field.has_default(),
            msg='`source` must have no default — provenance is always explicit.',
        )


# --- I: nothing creates these rows ---------------------------------------------------

class NoAutomaticOnboardingTests(TestCase):
    def test_creating_a_restaurant_creates_no_onboarding_row(self):
        # No post_save signal, no get_or_create, no backfill. A restaurant created
        # by ANY existing code path — including code that predates this domain —
        # stays outside it until an administrator explicitly brings it in.
        restaurant = _make_restaurant('Untouched House')

        self.assertEqual(RestaurantOnboarding.objects.count(), 0)
        with self.assertRaises(RestaurantOnboarding.DoesNotExist):
            restaurant.admin_onboarding

    def test_absence_is_the_state_of_every_pre_existing_restaurant(self):
        for name in ('Legacy One', 'Legacy Two', 'Legacy Three'):
            _make_restaurant(name)

        self.assertEqual(Restaurant.objects.count(), 3)
        self.assertEqual(RestaurantOnboarding.objects.count(), 0)


# --- J: provenance survives deletion attempts ----------------------------------------

class OnboardingIsProtectedEvidenceTests(_OnboardingFixture):
    def test_hard_deleting_the_restaurant_is_refused(self):
        self.legacy_adopted()

        # Restaurants are normally SOFT-deleted; a hard delete is not routine, and
        # it must not take the record of how this tenant entered Dinify with it.
        with self.assertRaises(ProtectedError):
            self.restaurant.delete()

        self.assertEqual(RestaurantOnboarding.objects.count(), 1)

    def test_deleting_the_adopting_administrator_is_refused(self):
        self.legacy_adopted()

        with self.assertRaises(ProtectedError):
            self.adopter.delete()

    def test_deleting_the_creating_administrator_is_refused(self):
        self.admin_created()

        with self.assertRaises(ProtectedError):
            self.creator.delete()

    def test_deleting_the_attesting_administrator_is_refused(self):
        self.legacy_adopted(**self.attestation())

        # An attestation whose attestor vanished is an assertion nobody made.
        with self.assertRaises(ProtectedError):
            self.attestor.delete()


# --- the columns this model deliberately does NOT have -------------------------------

class OnboardingHasNoDerivableStateTests(TestCase):
    """
    Claim state and go-live approval are absent BY DECISION, so their absence is
    asserted rather than left to be noticed.

    Claim is derived from evidence — a consumed ``OwnerInvitation``, or the
    attestation triple, or neither — and a stored ``claimed`` flag would be a fourth
    answer able to contradict all three. Go-live approval is a readiness input whose
    reset semantics (after a material setup change? after suspension? per lifecycle
    episode?) are not frozen; adding the column now would freeze them by accident.
    """

    def test_no_persisted_claim_state(self):
        forbidden = {
            'claimed', 'claim_status', 'claimed_at', 'owner_claimed_at',
            'claim_tracked', 'owner_claim_status',
        }
        self.assertEqual(
            forbidden & _concrete_field_names(RestaurantOnboarding), set(),
        )

    def test_no_go_live_approval_columns(self):
        forbidden = {
            'go_live_approved_at', 'go_live_approved_by', 'approval_reset_at',
            'approval_status', 'go_live_approval',
        }
        self.assertEqual(
            forbidden & _concrete_field_names(RestaurantOnboarding), set(),
        )

    def test_no_lifecycle_or_readiness_shadow(self):
        # `Restaurant.status` owns lifecycle and `check_go_live_readiness` owns
        # readiness. A copy of either here would be a second value able to drift.
        forbidden = {'status', 'lifecycle_state', 'readiness', 'readiness_state'}
        self.assertEqual(
            forbidden & _concrete_field_names(RestaurantOnboarding), set(),
        )


# --- invitation fixtures --------------------------------------------------------------

class _InvitationFixture(_IntegrityAssertions, TestCase):
    """One onboarding row plus the actors an invitation names."""

    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant('Invitation House')
        self.adopter = _make_staff('invitation-adopter')
        self.onboarding = RestaurantOnboarding.objects.create(
            restaurant=self.restaurant,
            source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(),
            adopted_by=self.adopter,
        )
        self.invited = self.restaurant.owner
        self.issuer = _make_staff('invitation-issuer')
        self._hashes = iter(f'{n:064x}' for n in range(1, 999))

    def invite(self, **overrides):
        """A well-formed pending invitation, with fields overridable per case."""
        now = timezone.now()
        payload = dict(
            onboarding=self.onboarding,
            invited_user=self.invited,
            issued_by=self.issuer,
            token_hash=next(self._hashes),
            issued_at=now,
            expires_at=now + timedelta(days=7),
        )
        payload.update(overrides)
        return OwnerInvitation.objects.create(**payload)


# --- A + B + M + N: what an invitation stores, and what it must not ------------------

class InvitationStorageShapeTests(_InvitationFixture):
    def test_token_hash_is_unique(self):
        second = RestaurantOnboarding.objects.create(
            restaurant=_make_restaurant('Second House'),
            source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(),
            adopted_by=self.adopter,
        )
        invitation = self.invite()

        # Two rows resolvable by one presented token would make redemption
        # ambiguous — the wrong tenant could be claimed with the right secret.
        with self.assertViolates():
            self.invite(onboarding=second, token_hash=invitation.token_hash)

    def test_only_the_hash_is_stored_never_the_raw_credential(self):
        names = _concrete_field_names(OwnerInvitation)

        self.assertIn('token_hash', names)
        forbidden = {
            'token', 'raw_token', 'plaintext_token', 'plain_token', 'claim_token',
            'claim_url', 'claim_link', 'secret', 'password', 'code',
        }
        self.assertEqual(forbidden & names, set())
        # Anything token-shaped must be THE hash. A future `token_preview` or
        # `token_last4` would be a partial credential leak by a friendlier name.
        self.assertEqual(
            {name for name in names if 'token' in name}, {'token_hash'},
        )

    def test_the_restaurant_is_derived_never_stored_a_second_time(self):
        invitation = self.invite()

        # ONE path to the tenant: invitation -> onboarding -> restaurant.
        self.assertEqual(
            invitation.onboarding.restaurant_id, self.restaurant.pk,
        )
        self.assertEqual(
            {field.name for field in OwnerInvitation._meta.fields
             if field.related_model is Restaurant},
            set(),
            msg='A second restaurant reference could drift from the onboarding row.',
        )
        self.assertEqual(
            [column for column in _column_names(OwnerInvitation)
             if 'restaurant' in column],
            [],
        )

    def test_no_identity_snapshot(self):
        # Identity stays on the canonical User; a copied email or phone goes stale
        # the moment the owner edits their profile, and then two answers exist.
        names = _concrete_field_names(OwnerInvitation)
        forbidden = {
            'email', 'phone', 'phone_number', 'msisdn', 'invited_email',
            'invited_phone', 'first_name', 'last_name', 'name',
        }
        self.assertEqual(forbidden & names, set())

    def test_no_delivery_state_columns(self):
        # Delivery is NOT modelled yet, deliberately: today's transactional email
        # and SMS paths are not reliable enough to freeze a contract around, and
        # the first implementation can hand a claim link over operator-mediated.
        names = _concrete_field_names(OwnerInvitation)
        forbidden = {
            'delivery_channel', 'delivery_state', 'delivery_status', 'email_status',
            'sms_status', 'delivered_at', 'sent_at', 'sent_to', 'channel',
            'provider_message_id', 'message_id', 'delivery_attempts',
        }
        self.assertEqual(forbidden & names, set())

    def test_no_persisted_expiry_or_status_column(self):
        # "Expired" is `expires_at <= now`, evaluated on read. A stored status
        # would need a sweeper to maintain it, and nothing in this repository runs
        # on a schedule — see BACKGROUND_TASKS.md.
        names = _concrete_field_names(OwnerInvitation)
        self.assertEqual({'status', 'state', 'expired'} & names, set())


# --- C + D + E + F: one unresolved invitation per onboarding -------------------------

class OneUnresolvedInvitationTests(_InvitationFixture):
    def test_a_second_pending_invitation_is_refused(self):
        self.invite()

        # Two live invitations means two people were asked to become the same
        # owner, and whichever arrives second silently wins.
        with self.assertViolates('one_unresolved_owner_invitation_per_onboarding'):
            self.invite()

    def test_a_consumed_invitation_frees_the_slot(self):
        self.invite(consumed_at=timezone.now())

        self.assertIsNotNone(self.invite().pk)

    def test_a_cancelled_invitation_frees_the_slot(self):
        self.invite(cancelled_at=timezone.now(), cancelled_by=self.issuer)

        self.assertIsNotNone(self.invite().pk)

    def test_a_superseded_invitation_frees_the_slot(self):
        self.invite(superseded_at=timezone.now())

        self.assertIsNotNone(self.invite().pk)

    def test_the_slot_is_per_onboarding_not_global(self):
        other = RestaurantOnboarding.objects.create(
            restaurant=_make_restaurant('Other House'),
            source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(),
            adopted_by=self.adopter,
        )
        self.invite()

        self.assertIsNotNone(self.invite(onboarding=other).pk)


# --- G: expiry is clock-derived, so an expired row still holds the slot ---------------

class ExpiredInvitationStillHoldsTheSlotTests(_InvitationFixture):
    def _expired(self):
        now = timezone.now()
        return self.invite(
            issued_at=now - timedelta(days=30),
            expires_at=now - timedelta(days=1),
        )

    def test_an_expired_invitation_is_unresolved_but_unclaimable(self):
        invitation = self._expired()

        self.assertTrue(invitation.is_expired)
        self.assertFalse(invitation.is_resolved)
        self.assertFalse(invitation.is_claimable)

    def test_an_expired_invitation_still_occupies_the_unresolved_slot(self):
        self._expired()

        # DELIBERATE, not an oversight. A PostgreSQL partial-index predicate must
        # be IMMUTABLE, so the unresolved-slot index cannot consult `now()`. The
        # reissue service must therefore supersede the stale row inside its own
        # transaction before inserting a replacement — exactly as
        # `challenges.create_challenge` consumes before it inserts.
        with self.assertViolates('one_unresolved_owner_invitation_per_onboarding'):
            self.invite()

    def test_superseding_the_expired_row_is_what_frees_it(self):
        expired = self._expired()

        OwnerInvitation.objects.filter(pk=expired.pk).update(
            superseded_at=timezone.now(),
        )

        self.assertIsNotNone(self.invite().pk)

    def test_a_live_invitation_is_claimable(self):
        invitation = self.invite()

        self.assertFalse(invitation.is_expired)
        self.assertFalse(invitation.is_resolved)
        self.assertTrue(invitation.is_claimable)


# --- H + I + J: terminal-state and timestamp integrity -------------------------------

class InvitationTerminalStateTests(_InvitationFixture):
    def test_consumed_and_cancelled_cannot_both_be_stamped(self):
        with self.assertViolates('owner_invitation_not_consumed_and_cancelled'):
            self.invite(
                consumed_at=timezone.now(),
                cancelled_at=timezone.now(),
                cancelled_by=self.issuer,
            )

    def test_consumed_and_superseded_cannot_both_be_stamped(self):
        with self.assertViolates('owner_invitation_not_consumed_and_superseded'):
            self.invite(
                consumed_at=timezone.now(), superseded_at=timezone.now(),
            )

    def test_cancelled_and_superseded_cannot_both_be_stamped(self):
        with self.assertViolates('owner_invitation_not_cancelled_and_superseded'):
            self.invite(
                cancelled_at=timezone.now(),
                cancelled_by=self.issuer,
                superseded_at=timezone.now(),
            )

    def test_a_resolved_invitation_cannot_be_re_resolved_differently(self):
        invitation = self.invite(consumed_at=timezone.now())

        # The same rule holds on UPDATE, not just INSERT — a redemption cannot be
        # retroactively recast as a cancellation.
        with self.assertViolates('owner_invitation_not_consumed_and_cancelled'):
            OwnerInvitation.objects.filter(pk=invitation.pk).update(
                cancelled_at=timezone.now(), cancelled_by=self.issuer,
            )

    def test_cancellation_timestamp_without_an_actor_is_refused(self):
        with self.assertViolates('owner_invitation_cancellation_pair'):
            self.invite(cancelled_at=timezone.now())

    def test_cancelling_actor_without_a_timestamp_is_refused(self):
        with self.assertViolates('owner_invitation_cancellation_pair'):
            self.invite(cancelled_by=self.issuer)


class InvitationExpiryWindowTests(_InvitationFixture):
    def test_expiry_before_issue_is_refused(self):
        now = timezone.now()
        # Born dead: it would read as expired forever while still holding the
        # unresolved slot, so nothing could be issued for that onboarding again.
        with self.assertViolates('owner_invitation_expires_after_issue'):
            self.invite(issued_at=now, expires_at=now - timedelta(seconds=1))

    def test_expiry_equal_to_issue_is_refused(self):
        now = timezone.now()
        with self.assertViolates('owner_invitation_expires_after_issue'):
            self.invite(issued_at=now, expires_at=now)


# --- K + L: an invitation is evidence too --------------------------------------------

class InvitationIsProtectedEvidenceTests(_InvitationFixture):
    def test_deleting_the_invited_user_is_refused(self):
        standalone = _make_user('invited-standalone@t.com')
        self.invite(invited_user=standalone)

        with self.assertRaises(ProtectedError):
            standalone.delete()

    def test_deleting_the_issuing_administrator_is_refused(self):
        self.invite()

        with self.assertRaises(ProtectedError):
            self.issuer.delete()

    def test_deleting_the_onboarding_row_is_refused(self):
        self.invite()

        # The onboarding row is the context that makes the credential meaningful;
        # an orphaned invitation could not say which tenant it was for.
        with self.assertRaises(ProtectedError):
            self.onboarding.delete()

    def test_deleting_the_cancelling_administrator_is_refused(self):
        canceller = _make_staff('invitation-canceller')
        self.invite(cancelled_at=timezone.now(), cancelled_by=canceller)

        with self.assertRaises(ProtectedError):
            canceller.delete()
