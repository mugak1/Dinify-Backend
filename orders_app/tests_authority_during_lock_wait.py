"""
D06 completion, G1b — THE AUTHORITY THAT DECIDES IS THE AUTHORITY THAT HOLDS
NOW, NOT THE ONE THAT HELD WHEN THE REQUEST ARRIVED.

Every protected boundary here resolves its caller in AUTOCOMMIT and then WAITS —
for the admission advisory lock, the table row and the order row. D06 already
carried the diner's capability across that wait and re-verified the QR
generation under the lock. Two facts were left behind on the other side of it.

  1. A STAFF CALLER'S AUTHORITY WAS NEVER RE-ASKED. The endpoint's comment said
     so in terms: "No capability channel was used, so there is nothing to
     re-verify. A staff caller's authority is the module gate above, which is
     not revoked by a QR regeneration." True and beside the point — it is
     revoked by a membership being deactivated, a role being taken away or a
     restaurant leaving the portal-access states, and any of those can commit
     inside the wait. The order then reaches the kitchen on authority that no
     longer exists.

  2. RETIREMENT NEVER RE-ASKED WHETHER THE DINER'S SESSION STILL EXISTED. A
     table that stops being scannable REVOKES every session on it, which is why
     `_resolve_table` re-checks it live on every use and the endpoint answers the
     channel's opaque 404. Under the lock, nothing asked again — and retirement
     writes a CLOSURE, which is irreversible.

THE FACT IS CHECKED AT BOTH PROTECTED DECISIONS; THE ANSWER STAYS EACH ROUTE'S
OWN. Acceptance already re-reads scannability from the LOCKED table through
`order_eligibility` and refuses with a sentence a diner can read; that is pinned
here rather than assumed. Retirement runs no eligibility rule by design, and its
established answer for this fact — at the endpoint, a moment earlier — is the
opaque 404. Answering differently purely because of WHEN the operator clicked is
the inconsistency this gate exists to remove.

WHAT IS NOT CARRIED. No token, no client-selectable actor flag and no
trusted-caller switch: `StaffAuthority` holds the principal, the
server-resolved restaurant and the module name, and the re-assertion is the same
resolver call the endpoint made. It can only ever REFUSE.
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    MODULE_TABLES, OrderStatus_Pending, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers.manage_order import (
    retire_quote_for_review, update_order_status,
)
from orders_app.controllers.services import order_eligibility as eligibility
from orders_app.controllers.services.order_authority import StaffAuthority
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderAcceptance, OrderQuoteClosure
from orders_app.tests_quote_lifetime import QuoteFixtureMixin
from restaurants_app.controllers import diner_capability
from restaurants_app.controllers.diner_capability import capability_from_table
from restaurants_app.models import (
    MenuItem, Restaurant, RestaurantEmployee, Table,
)


def _during_the_wait(effect):
    """Fire ``effect`` in the real window: after the admission advisory lock and
    BEFORE the table and order rows are locked and read.

    THE POSITION IS THE WHOLE POINT, and the obvious later seam is WRONG. The
    first cut of this helper wrapped `assert_capability_current`, which runs
    once both rows are held — and once a boundary holds the `Table` row a
    competing writer CANNOT commit against it, because that is exactly what the
    row lock is for. A change injected there models nothing that can happen in
    production: the in-memory row is authoritative for the rest of the
    transaction, so the suite would have passed or failed on an artefact. The
    window a real revocation lands in is THE WAIT, and the wait ends when the
    row lock is granted.

    The advisory lock is the first statement of both transactions, so wrapping
    it lands the competing change deterministically at the top of the ordering —
    no sleep, no second connection, no timing. Both boundaries reach it by a
    different binding (`_submit_order` through `order_admission.admit`, the
    retire route through its own local import), so both names are patched and
    the effect fires at most ONCE.
    """
    from orders_app.controllers.services import order_admission
    from restaurants_app.controllers import admission_lock

    state = {'fired': False}

    def _wrap(real):
        def _wrapped(*args, **kwargs):
            result = real(*args, **kwargs)
            if not state['fired']:
                state['fired'] = True
                effect()
            return result
        return _wrapped

    return _patch_both(
        patch.object(order_admission, 'lock_admission_shared',
                     _wrap(order_admission.lock_admission_shared)),
        patch.object(admission_lock, 'lock_admission_shared',
                     _wrap(admission_lock.lock_admission_shared)),
    )


class _patch_both:
    """Two patches, one ``with``. Deliberately tiny — a nested context manager
    would read as though the order mattered, and it does not."""

    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class AuthorityFixture(QuoteFixtureMixin, TestCase):

    def _staff_authority(self, order):
        return StaffAuthority(
            user=self.owner,
            restaurant_id=str(order.restaurant_id),
            module=MODULE_TABLES,
        )

    def _revoke_membership(self):
        RestaurantEmployee.objects.filter(
            user=self.owner, restaurant=self.restaurant,
        ).update(active=False)

    def _strip_roles(self):
        RestaurantEmployee.objects.filter(
            user=self.owner, restaurant=self.restaurant,
        ).update(roles=[])

    def _unscannable(self):
        Table.objects.filter(pk=self.table.pk).update(
            status='out_of_service', is_active=False)

    def _submit_as_staff(self, order):
        return update_order_status(
            order, OrderStatus_Pending, self.owner,
            quote_ref=quote_ref(order),
            authority=self._staff_authority(order),
        )

    def _retire_as_staff(self, order):
        return retire_quote_for_review(
            order, quote_ref(order), authority=self._staff_authority(order),
        )

    def _retire_as_diner(self, order):
        return retire_quote_for_review(
            order, quote_ref(order),
            capability=capability_from_table(self.table),
        )


class StaffAuthorityIsReAskedUnderTheLockTests(AuthorityFixture):
    """THE REGRESSION. A staff-origin submission on revoked authority."""

    def test_a_membership_deactivated_during_the_wait_refuses_the_submit(self):
        order = self._draft_order(created_by=self.owner)
        with _during_the_wait(self._revoke_membership):
            refused = self._submit_as_staff(order)

        self.assertEqual(refused.get('status'), 404, refused)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())

    def test_roles_taken_away_during_the_wait_refuse_the_submit(self):
        order = self._draft_order(created_by=self.owner)
        with _during_the_wait(self._strip_roles):
            refused = self._submit_as_staff(order)

        self.assertEqual(refused.get('status'), 404, refused)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_a_membership_deactivated_during_the_wait_refuses_the_retirement(self):
        order = self._draft_order(created_by=self.owner)
        with _during_the_wait(self._revoke_membership):
            refused = self._retire_as_staff(order)

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'a revoked principal must not write an irreversible closure',
        )

    def test_the_refusal_is_the_endpoints_own_non_disclosing_404(self):
        """Not a new vocabulary: the same answer the module gate gives, so a
        revocation landing mid-request is indistinguishable from never having
        had access."""
        order = self._draft_order(created_by=self.owner)
        with _during_the_wait(self._revoke_membership):
            refused = self._submit_as_staff(order)

        self.assertEqual(
            set(refused), {'status', 'message'},
            'a non-disclosing refusal carries nothing else',
        )
        self.assertEqual(refused['message'], 'Not found')

    # -- the controls ------------------------------------------------------

    def test_the_control_intact_authority_still_submits(self):
        order = self._draft_order(created_by=self.owner)
        self.assertEqual(self._submit_as_staff(order).get('status'), 200)

    def test_the_control_intact_authority_still_retires_nothing(self):
        order = self._draft_order(created_by=self.owner)
        result = self._retire_as_staff(order)
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result.get('outcome'), 'quote_still_valid', result)

    def test_the_control_a_caller_that_carries_no_authority_is_unchanged(self):
        """An in-process caller with neither channel keeps exactly the guarantees
        it had. This is not a new requirement to hold a credential."""
        order = self._draft_order(created_by=self.owner)
        self.assertEqual(
            update_order_status(
                order, OrderStatus_Pending, self.owner,
                quote_ref=quote_ref(order),
            ).get('status'),
            200,
        )

    def test_the_diner_path_needs_no_staff_authority(self):
        order = self._draft_order()
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
            capability=capability_from_table(self.table),
        )
        self.assertEqual(result.get('status'), 200, result)


class ScanAvailabilityIsCheckedAtTheProtectedDecisionTests(AuthorityFixture):
    """The same fact at both boundaries, each answering in its own words."""

    def test_acceptance_refuses_a_table_taken_out_of_service_during_the_wait(self):
        """Already true through `order_eligibility`, and pinned so it stays
        true: acceptance reads scannability off the row it LOCKED."""
        order = self._draft_order()
        with _during_the_wait(self._unscannable):
            refused = update_order_status(
                order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
                capability=capability_from_table(self.table),
            )

        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_TABLE_UNAVAILABLE)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_retirement_refuses_a_table_taken_out_of_service_during_the_wait(self):
        """THE REGRESSION. Retirement runs no eligibility rule by design, so
        nothing re-asked whether the session that arrived still existed — and it
        writes a closure that cannot be undone."""
        order = self._draft_order()
        with _during_the_wait(self._unscannable):
            refused = self._retire_as_diner(order)

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'a revoked session must not retire a quote',
        )

    def test_the_answer_is_the_channels_own_404_not_a_new_word(self):
        order = self._draft_order()
        with _during_the_wait(self._unscannable):
            refused = self._retire_as_diner(order)
        self.assertEqual(set(refused), {'status', 'message'}, refused)

    # -- the controls ------------------------------------------------------

    def test_the_control_a_live_table_still_retires_normally(self):
        order = self._draft_order()
        result = self._retire_as_diner(order)
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result.get('outcome'), 'quote_still_valid', result)

    def test_the_control_a_PAUSED_restaurant_can_still_retire(self):
        """D06's asymmetry must survive: a pause is exactly when a client most
        needs to establish that its held quote is dead."""
        from restaurants_app.models import Restaurant
        order = self._draft_order()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        result = self._retire_as_diner(order)
        self.assertEqual(result.get('status'), 200, result)

    def test_the_control_a_staff_caller_is_not_bound_by_the_SESSION_rule(self):
        """Scannability revokes a diner SESSION. A staff caller holds no session,
        so the rule has nothing to revoke — retiring a quote at a table that has
        been taken out of service is exactly what an operator would want, and
        refusing would buy the diner nothing."""
        order = self._draft_order(created_by=self.owner)
        self._unscannable()

        result = self._retire_as_staff(order)
        self.assertEqual(result.get('status'), 200, result)


class TheEndpointCarriesTheAuthorityTests(AuthorityFixture):
    """OVER THE REAL ROUTE, because the service cannot prove this on its own.

    Every test above hands `update_order_status` / `retire_quote_for_review` a
    `StaffAuthority` directly, which proves the BOUNDARY honours one. It says
    nothing about whether the endpoint that resolves the staff caller actually
    builds one — and that was precisely the gap: the module gate ran in
    autocommit and the request then travelled to the transition carrying
    nothing. A service-level test would have passed against that tree forever.

    So these drive `PUT api/v1/orders/<action>/` with a real bearer token and a
    real module gate, and revoke the membership inside the wait.
    """

    SUBMIT_URL = '/api/v1/orders/submit/'
    RETIRE_URL = '/api/v1/orders/retire-quote/'

    def _jwt(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(self.owner).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _put(self, url, order):
        import json
        return self.client.put(
            url,
            data=json.dumps({
                'order': str(order.pk), 'quote_ref': quote_ref(order),
            }),
            content_type='application/json',
            **self._jwt(),
        )

    def test_a_membership_revoked_during_the_wait_refuses_the_HTTP_submit(self):
        order = self._draft_order()
        with _during_the_wait(self._revoke_membership):
            response = self._put(self.SUBMIT_URL, order)

        self.assertEqual(response.status_code, 404, response.content)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())

    def test_a_membership_revoked_during_the_wait_refuses_the_HTTP_retirement(self):
        order = self._draft_order()
        with _during_the_wait(self._revoke_membership):
            response = self._put(self.RETIRE_URL, order)

        self.assertEqual(response.status_code, 404, response.content)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'a revoked membership must not retire a quote',
        )

    def test_the_HTTP_refusal_discloses_nothing_beyond_the_channels_404(self):
        order = self._draft_order()
        with _during_the_wait(self._revoke_membership):
            response = self._put(self.SUBMIT_URL, order)
        self.assertEqual(
            set(response.json()), {'status', 'message'}, response.content)

    # -- the controls ------------------------------------------------------

    def test_the_control_an_intact_membership_still_submits_over_HTTP(self):
        """The gate can only ever REFUSE, so an unchanged world is unchanged."""
        order = self._draft_order()
        response = self._put(self.SUBMIT_URL, order)
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)

    def test_the_control_an_intact_membership_still_retires_over_HTTP(self):
        order = self._draft_order()
        response = self._put(self.RETIRE_URL, order)
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_control_the_diner_channel_is_untouched_over_HTTP(self):
        """The diner route builds no `StaffAuthority` and must keep working
        exactly as it did — the staff re-check is additive, not a new
        requirement to hold a module."""
        import json
        from restaurants_app.controllers.diner_capability import (
            SESSION_HEADER, issue_table_session,
        )
        order = self._draft_order()
        header = 'HTTP_' + SESSION_HEADER.upper().replace('-', '_')
        response = self.client.put(
            self.SUBMIT_URL,
            data=json.dumps({
                'order': str(order.pk), 'quote_ref': quote_ref(order),
            }),
            content_type='application/json',
            **{header: issue_table_session(self.table)},
        )
        self.assertEqual(response.status_code, 200, response.content)


def _after_the_intent_lookup(effect):
    """Fire ``effect`` between ``_create_order``'s intent lookup and its
    authority re-check, which is the window a REPLAY disclosure sits in.

    WHY NOT `_during_the_wait`. That helper wraps the admission advisory lock,
    which `_create_order` takes at step 1a — AFTER step 1's replay lookup and
    the return it can make. A replay never reaches the advisory lock at all (it
    is deliberately exempt, so recovery never queues behind live ordering), so
    an effect injected there would never fire for the case under test and the
    suite would pass by not running.

    `resolve_intent` is the last thing before the disclosure, so wrapping it
    lands the revocation exactly where a real one lands: after this request
    decided it has a replay to hand back, before it decides whether the caller
    may still see it. Patched in `create_order`'s own namespace, because that is
    the binding the service calls through.
    """
    from orders_app.controllers.services import create_order as service

    state = {'fired': False}
    real = service.resolve_intent

    def _wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        if not state['fired'] and getattr(result, 'is_match', False):
            state['fired'] = True
            effect()
        return result

    return patch.object(service, 'resolve_intent', _wrapped)


class CreationReAsksTheAuthorityUnderTheLockTests(AuthorityFixture):
    """A1(A) — THE REGRESSION. Creation carried no authority at all.

    The acceptance boundary has re-asked this question since G1b; creation had
    neither half of it. So a draft was written — and a daily ticket number spent
    on it — for a diner whose session the owner had just revoked, or a staff
    member whose membership had just been deactivated, and only the LATER
    acceptance refused it.
    """

    def _regenerate_qr(self):
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)

    def _create_as_diner(self, **kwargs):
        from orders_app.controllers.services.create_order import _create_order
        return _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            capability=capability_from_table(self.table),
            **kwargs,
        )

    def _create_as_staff(self, **kwargs):
        from orders_app.controllers.services.create_order import _create_order
        return _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            created_by=self.owner,
            authority=StaffAuthority(
                user=self.owner, restaurant_id=str(self.restaurant.pk),
                module=MODULE_TABLES,
            ),
            **kwargs,
        )

    # -- the diner half ----------------------------------------------------

    def test_a_QR_regenerated_during_the_wait_refuses_the_creation(self):
        before = Order.objects.count()
        with _during_the_wait(self._regenerate_qr):
            refused = self._create_as_diner()

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertEqual(Order.objects.count(), before,
                         'a revoked session must not write a draft')

    def test_and_spends_no_daily_ticket_number(self):
        """The counter is a CREATION EFFECT. A refusal that still consumed one
        would leave a gap in the restaurant's numbering for an order nobody
        placed — the same reasoning D04 records about a replay."""
        from orders_app.models import RestaurantDailyOrderCounter
        with _during_the_wait(self._regenerate_qr):
            self._create_as_diner()

        self.assertFalse(
            RestaurantDailyOrderCounter.objects.filter(
                restaurant=self.restaurant).exists(),
            'no number may be allocated for a refused creation',
        )

    def test_the_refusal_is_the_capability_channels_own_opaque_404(self):
        with _during_the_wait(self._regenerate_qr):
            refused = self._create_as_diner()

        self.assertEqual(set(refused), {'status', 'message'})
        self.assertEqual(refused['status'], 404)

    # -- the staff half ----------------------------------------------------

    def test_a_membership_deactivated_during_the_wait_refuses_the_creation(self):
        before = Order.objects.count()
        with _during_the_wait(self._revoke_membership):
            refused = self._create_as_staff()

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertEqual(Order.objects.count(), before)

    def test_roles_taken_away_during_the_wait_refuse_the_creation(self):
        with _during_the_wait(self._strip_roles):
            refused = self._create_as_staff()
        self.assertEqual(refused.get('status'), 404, refused)

    def test_a_carried_authority_naming_ANOTHER_restaurant_is_refused(self):
        """The staff counterpart of `assert_capability_current`'s identity check.

        The endpoint authorized a principal against ONE restaurant; this service
        loaded the restaurant it is about to create at. Re-asking the module gate
        about a different row would authorize the wrong thing, so the two must
        name the same restaurant before the gate is consulted at all.
        """
        from orders_app.controllers.services.create_order import _create_order
        other = Restaurant.objects.create(
            name='Elsewhere', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        refused = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            created_by=self.owner,
            authority=StaffAuthority(
                user=self.owner, restaurant_id=str(other.pk),
                module=MODULE_TABLES,
            ),
        )
        self.assertEqual(refused.get('status'), 404, refused)

    # -- the controls ------------------------------------------------------

    def test_the_control_an_intact_session_still_creates(self):
        result = self._create_as_diner()
        self.assertEqual(result.get('status'), 200, result)

    def test_the_control_intact_staff_authority_still_creates(self):
        result = self._create_as_staff()
        self.assertEqual(result.get('status'), 200, result)

    def test_the_control_a_caller_that_carries_NEITHER_is_unchanged(self):
        """An in-process caller keeps exactly the guarantees it had. This is a
        RE-assertion, never a new requirement to hold a credential."""
        result = self._draft()
        self.assertEqual(result.get('status'), 200, result)

    def test_the_control_a_revocation_AFTER_the_check_is_not_claimed_away(self):
        """The honest limit, stated rather than implied: the decision linearizes
        at the re-check, so a revocation committing after it can still overlap.
        What is promised is that nothing decided BEFORE that point is acted on."""
        result = self._create_as_diner()
        self.assertEqual(result.get('status'), 200, result)
        self._regenerate_qr()
        # The draft stands — and the acceptance boundary is what refuses it.
        self.assertEqual(Order.objects.filter(pk=result['order'].pk).count(), 1)


class ReplayDisclosureIsAuthorizedTests(AuthorityFixture):
    """A1(B) — A REPLAY IS EXEMPT FROM NEW-ORDER POLICY, NOT FROM AUTHORIZATION.

    The replay branch returns an existing order, and since G3a the closure
    recorded against it. It did so without re-asking whether the caller may
    still see either — so a revoked session could read back an order and its
    retirement, on a credential the owner had just killed.

    The exemption it keeps is the right one: a pause, menu-only ordering, an
    item that has sold out and a quote that has expired are all statements about
    NEW work, and refusing an order that was already created and acknowledged on
    the strength of them is the retroactive refusal D04 exists to stop.
    """

    def _with_key(self):
        from orders_app.controllers.services.create_order import _create_order
        result = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id='11111111-1111-4111-8111-111111111111',
            capability=capability_from_table(self.table),
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def _replay(self, **kwargs):
        from orders_app.controllers.services.create_order import _create_order
        return _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id='11111111-1111-4111-8111-111111111111',
            **kwargs,
        )

    def test_THE_REGRESSION_a_revoked_session_is_not_handed_the_replay(self):
        order = self._with_key()
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)

        refused = self._replay(capability=capability_from_table(self.table))
        # The capability was built from the PRE-regeneration row, which is what
        # a client holding a session presents.
        self.assertEqual(refused.get('status'), 404, refused)
        self.assertNotIn('order', refused)
        self.assertEqual(Order.objects.filter(pk=order.pk).count(), 1,
                         'the refusal changes nothing about the order')

    def test_a_revocation_landing_INSIDE_the_replay_window_is_caught(self):
        order = self._with_key()
        capability = capability_from_table(self.table)

        with _after_the_intent_lookup(
            lambda: Table.objects.filter(pk=self.table.pk).update(
                qr_version=self.table.qr_version + 1)
        ):
            refused = self._replay(capability=capability)

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertEqual(Order.objects.filter(pk=order.pk).count(), 1)

    def test_a_staff_membership_revoked_before_a_replay_is_refused(self):
        # THE ORIGINAL IS CREATED BY THE SAME PRINCIPAL. D04's intent binding
        # includes PROVENANCE, so a diner-origin order replayed as staff is a
        # `checkout_intent_mismatch` long before authorization is reached —
        # correct, and a different rule from the one under test here.
        from orders_app.controllers.services.create_order import _create_order
        staff_authority = StaffAuthority(
            user=self.owner, restaurant_id=str(self.restaurant.pk),
            module=MODULE_TABLES,
        )
        first = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id='11111111-1111-4111-8111-111111111111',
            created_by=self.owner, authority=staff_authority,
        )
        self.assertEqual(first.get('status'), 200, first)
        self._revoke_membership()

        refused = self._replay(
            created_by=self.owner, authority=staff_authority,
        )
        self.assertEqual(refused.get('status'), 404, refused)

    # -- what a replay must STILL be exempt from ---------------------------

    def test_a_PAUSED_restaurant_still_replays(self):
        """`accepting_orders` is a statement about NEW diner ordering. An order
        already created and acknowledged is not new work."""
        order = self._with_key()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        result = self._replay(capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))
        self.assertEqual(result['order'].pk, order.pk)

    # -- A1b: SCANNABILITY REVOKES A SESSION, AND A REPLAY IS A DISCLOSURE --
    #
    # THE ORACLE THAT USED TO LIVE HERE ASSERTED THE OPPOSITE. It was called
    # `test_a_table_taken_out_of_service_still_replays` and it filed
    # scannability under "what a replay must STILL be exempt from", beside the
    # pause and the sold-out item. Those two really are statements about NEW
    # work. Scannability is not: `_resolve_table` re-reads it live on every use
    # and treats a table that has stopped being scannable as a REVOKED SESSION,
    # answering the channel's opaque 404 — so the endpoint refuses such a
    # request outright, and the branch below is reachable only when the table
    # goes out of service after that resolution. Disclosing an order and its
    # closure to a session the door would no longer admit is an authorization
    # failure, not an exemption.
    #
    # The old test was still asserting something TRUE, about a caller it was
    # not written for: an in-process call carries no capability, so there is no
    # session for a table to revoke. That case is preserved below, as a control
    # saying which caller it is an oracle for.

    def test_THE_REGRESSION_an_unscannable_table_does_not_replay_to_a_diner(self):
        order = self._with_key()
        self._unscannable()

        refused = self._replay(capability=capability_from_table(self.table))
        self.assertEqual(refused.get('status'), 404, refused)
        self.assertNotIn('order', refused)
        self.assertEqual(
            Order.objects.filter(pk=order.pk).count(), 1,
            'the refusal changes nothing about the order',
        )

    def test_the_replay_refusal_is_the_channels_own_404_not_a_new_word(self):
        self._with_key()
        self._unscannable()
        refused = self._replay(capability=capability_from_table(self.table))
        self.assertEqual(set(refused), {'status', 'message'}, refused)

    def test_a_table_going_out_of_service_INSIDE_the_window_is_caught(self):
        """The window this actually sits in. The endpoint resolved a scannable
        table; the change commits while `resolve_intent` waits on the order row."""
        order = self._with_key()
        capability = capability_from_table(self.table)

        with _after_the_intent_lookup(self._unscannable):
            refused = self._replay(capability=capability)

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertEqual(Order.objects.filter(pk=order.pk).count(), 1)

    def test_a_RETIRED_quote_is_not_disclosed_to_a_revoked_session_either(self):
        """The disclosure that made this matter: since G3a a replay carries the
        closure recorded against the order, which is the one thing a client that
        lost the refusal needs. A session the door would no longer admit may not
        read it."""
        from orders_app.controllers.services import quote_closure
        order = self._with_key()
        with transaction.atomic():
            quote_closure.close(
                order, quote_ref=quote_ref(order),
                reason=quote_closure.REASON_EXPIRED,
                now=timezone.now(), evidence=None,
            )
        self._unscannable()

        refused = self._replay(capability=capability_from_table(self.table))
        self.assertEqual(refused.get('status'), 404, refused)
        self.assertNotIn('quote_closure', refused)

    def test_the_control_an_unscannable_table_STILL_replays_with_NO_capability(self):
        """What the old oracle was really about. An in-process caller holds no
        table session, so scannability has nothing to revoke — and this is a
        RE-assertion, never a new requirement to present a credential."""
        order = self._with_key()
        self._unscannable()

        result = self._replay()
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))
        self.assertEqual(result['order'].pk, order.pk)

    def test_the_control_an_unscannable_table_STILL_replays_for_STAFF(self):
        """A staff caller is authorized by the module gate, not by a table
        session, so the same reasoning applies — and refusing would buy the
        diner nothing."""
        from orders_app.controllers.services.create_order import _create_order
        staff_authority = StaffAuthority(
            user=self.owner, restaurant_id=str(self.restaurant.pk),
            module=MODULE_TABLES,
        )
        first = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id='11111111-1111-4111-8111-111111111111',
            created_by=self.owner, authority=staff_authority,
        )
        self.assertEqual(first.get('status'), 200, first)
        self._unscannable()

        result = self._replay(created_by=self.owner, authority=staff_authority)
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['order'].pk, first['order'].pk)

    def test_the_control_a_MENU_ONLY_table_still_replays(self):
        """`qr_mode` is ORDERING POLICY, which `is_available_for_scan` does not
        read and `order_eligibility` owns. A menu-only table still MINTS
        sessions, so nothing about the caller's session has been revoked."""
        order = self._with_key()
        Table.objects.filter(pk=self.table.pk).update(qr_mode='menu_only')

        result = self._replay(capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['order'].pk, order.pk)

    def test_the_control_a_valid_RESCAN_still_replays(self):
        """The refusal is about the SESSION, not about the replay. A diner who
        re-scans the regenerated code holds a current capability and recovers
        the order they already placed."""
        order = self._with_key()
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)
        rescanned = capability_from_table(Table.objects.get(pk=self.table.pk))

        result = self._replay(capability=rescanned)
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['order'].pk, order.pk)

    def test_an_item_gone_out_of_stock_still_replays(self):
        order = self._with_key()
        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=False)

        result = self._replay(capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['order'].pk, order.pk)

    def test_a_RETIRED_quote_still_replays_and_the_closure_is_disclosed(self):
        """The recovery this exists for. A closure is exactly what a client that
        lost the refusal needs to learn, and it learns it HERE."""
        from orders_app.controllers.services import quote_closure
        order = self._with_key()
        with transaction.atomic():
            quote_closure.close(
                order, quote_ref=quote_ref(order),
                reason=quote_closure.REASON_EXPIRED,
                now=timezone.now(), evidence=None,
            )

        result = self._replay(capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['order'].pk, order.pk)

    # -- an unexpected database error is NOT an authorization answer ------

    def test_an_OPERATIONAL_error_on_the_replay_table_read_PROPAGATES(self):
        """A lost connection is not a revoked capability.

        The lock-free replay read used to sit behind a bare ``except
        Exception``, so an ``OperationalError`` — a dropped connection, a
        statement timeout — became ``None``, which `assert_capability_current`
        reads as a revocation and the endpoint answers with the capability
        channel's opaque 404. The diner would be told their table session was no
        longer valid, and we would see an authorization event, for an outage.

        The handler names the two conditions it is actually for; everything else
        must reach ordinary error handling.
        """
        from django.db import OperationalError
        self._with_key()
        capability = capability_from_table(self.table)

        with patch.object(
            Table.objects, 'get',
            side_effect=OperationalError('server closed the connection'),
        ):
            with self.assertRaises(OperationalError):
                self._replay(capability=capability)

    def test_a_PROGRAMMING_error_on_the_replay_table_read_PROPAGATES(self):
        """The same for a query defect: a 404 would hide it indefinitely."""
        from django.db import ProgrammingError
        self._with_key()
        capability = capability_from_table(self.table)

        with patch.object(
            Table.objects, 'get',
            side_effect=ProgrammingError('column does not exist'),
        ):
            with self.assertRaises(ProgrammingError):
                self._replay(capability=capability)

    def test_THE_CONTROL_a_vanished_table_is_still_the_opaque_404(self):
        """The condition the handler IS for, unchanged by narrowing it.

        A table a diner's session names and that no longer exists is not a table
        they hold a session for, so this stays a revocation rather than an
        error.
        """
        self._with_key()
        capability = capability_from_table(self.table)

        with patch.object(
            Table.objects, 'get', side_effect=Table.DoesNotExist,
        ):
            refused = self._replay(capability=capability)

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertNotIn('order', refused)

    def test_THE_CONTROL_a_keyless_replay_never_reads_the_table_at_all(self):
        """No capability presented, so the read is skipped and an error in it
        cannot be reached — which is what keeps the pinned query budget flat for
        a keyless caller."""
        from django.db import OperationalError
        self._with_key()

        with patch.object(
            Table.objects, 'get',
            side_effect=OperationalError('must not be reached'),
        ):
            result = self._replay()

        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))

    def test_the_control_a_keyless_caller_reaches_no_replay_check(self):
        """No key, no replay: the check costs nothing and changes nothing on the
        path a diner without an idempotency key takes."""
        from orders_app.controllers.services.create_order import _create_order
        result = _create_order(
            restaurant=self.restaurant, table=self.table_b,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            capability=capability_from_table(self.table_b),
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertFalse(result.get('idempotent'))


class AcceptedSubmissionReplayRespectsTheSessionTests(AuthorityFixture):
    """A1b, the other half — THE ACCEPTED-SUBMISSION REPLAY.

    `_submit_order` re-verifies the capability's GENERATION under the lock and
    then answers D04's replay before any eligibility rule runs. Generation is
    only half of what revokes a session: a table taken out of service stops
    minting sessions altogether, which is why `_resolve_table` re-reads
    `is_available_for_scan()` on every use and answers the channel's opaque 404.

    So a diner whose table went out of service during the lock wait was still
    handed back the acceptance and its correlated projection — order id, table,
    restaurant, the exact `quote_ref` the diner confirmed — on a session the
    door would no longer admit. `retire_quote_for_review` already asks this
    question under the lock (G1b); the acceptance replay did not, for the same
    reason retirement did not: no eligibility rule runs before the return.

    THE EXEMPTION IT KEEPS IS UNCHANGED. A pause, menu-only ordering, a dish
    that has since sold out and a quote that has since expired are all
    statements about NEW work, and a replay stays exempt from every one of them.
    """

    def _accepted(self, capability=None):
        order = self._draft_order()
        ref = quote_ref(order)
        result = update_order_status(
            order, OrderStatus_Pending, None,
            quote_ref=ref, capability=capability,
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(OrderAcceptance.objects.filter(order=order).exists())
        order.refresh_from_db()
        return order, ref

    def _replay(self, order, ref, **kwargs):
        return update_order_status(
            order, OrderStatus_Pending, None, quote_ref=ref, **kwargs)

    def test_THE_REGRESSION_an_unscannable_table_is_not_handed_the_replay(self):
        order, ref = self._accepted(capability_from_table(self.table))

        with _during_the_wait(self._unscannable):
            refused = self._replay(
                order, ref, capability=capability_from_table(self.table))

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertNotIn('checkout', refused)
        self.assertNotIn('idempotent', refused)

    def test_the_refusal_is_the_channels_own_404_not_a_new_word(self):
        order, ref = self._accepted(capability_from_table(self.table))
        with _during_the_wait(self._unscannable):
            refused = self._replay(
                order, ref, capability=capability_from_table(self.table))
        self.assertEqual(set(refused), {'status', 'message'}, refused)

    def test_the_acceptance_evidence_is_untouched_by_the_refusal(self):
        """A refusal is not a second acceptance and not a retraction of the
        first: the submission DID land, and the row that says so never moves."""
        order, ref = self._accepted(capability_from_table(self.table))
        before = OrderAcceptance.objects.get(order=order)

        with _during_the_wait(self._unscannable):
            self._replay(
                order, ref, capability=capability_from_table(self.table))

        after = OrderAcceptance.objects.get(order=order)
        self.assertEqual(
            OrderAcceptance.objects.filter(order=order).count(), 1)
        self.assertEqual(after.accepted_at, before.accepted_at)
        self.assertEqual(after.quote_ref, before.quote_ref)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'a refused replay writes nothing',
        )

    def test_a_table_already_out_of_service_is_refused_too(self):
        """The boundary self-guards rather than trusting the endpoint that
        normally refuses this a moment earlier — the same rule `_create_order`
        follows for menu publication and lifecycle."""
        order, ref = self._accepted(capability_from_table(self.table))
        self._unscannable()

        refused = self._replay(
            order, ref, capability=capability_from_table(self.table))
        self.assertEqual(refused.get('status'), 404, refused)

    # -- the controls ------------------------------------------------------

    def test_the_control_an_intact_session_still_replays(self):
        order, ref = self._accepted(capability_from_table(self.table))

        result = self._replay(
            order, ref, capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))
        self.assertIn('checkout', result)

    def test_the_control_a_PAUSED_restaurant_still_replays(self):
        """D04's rule, unchanged: refusing an acceptance that already happened
        because NEW work is now disallowed is the retroactive refusal it exists
        to stop."""
        order, ref = self._accepted(capability_from_table(self.table))
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        result = self._replay(
            order, ref, capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))

    def test_the_control_a_MENU_ONLY_table_still_replays(self):
        order, ref = self._accepted(capability_from_table(self.table))
        Table.objects.filter(pk=self.table.pk).update(qr_mode='menu_only')

        result = self._replay(
            order, ref, capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)

    def test_the_control_an_item_gone_out_of_stock_still_replays(self):
        order, ref = self._accepted(capability_from_table(self.table))
        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=False)

        result = self._replay(
            order, ref, capability=capability_from_table(self.table))
        self.assertEqual(result.get('status'), 200, result)

    def test_the_control_a_caller_with_NO_capability_is_unchanged(self):
        """An in-process caller holds no table session, so there is none to
        revoke — and this must not become a new requirement to hold one."""
        order, ref = self._accepted()
        self._unscannable()

        result = self._replay(order, ref)
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))

    def test_the_control_a_STAFF_replay_is_not_bound_by_the_session_rule(self):
        order = self._draft_order(created_by=self.owner)
        ref = quote_ref(order)
        authority = self._staff_authority(order)
        first = update_order_status(
            order, OrderStatus_Pending, self.owner,
            quote_ref=ref, authority=authority,
        )
        self.assertEqual(first.get('status'), 200, first)
        order.refresh_from_db()
        self._unscannable()

        result = update_order_status(
            order, OrderStatus_Pending, self.owner,
            quote_ref=ref, authority=authority,
        )
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))

    def test_the_control_a_valid_RESCAN_still_replays(self):
        order, ref = self._accepted(capability_from_table(self.table))
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)
        rescanned = capability_from_table(Table.objects.get(pk=self.table.pk))

        result = self._replay(order, ref, capability=rescanned)
        self.assertEqual(result.get('status'), 200, result)
        self.assertTrue(result.get('idempotent'))
