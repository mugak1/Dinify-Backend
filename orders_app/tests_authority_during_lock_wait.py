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

from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    MODULE_TABLES, OrderStatus_Pending, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers.manage_order import (
    retire_quote_for_review, update_order_status,
)
from orders_app.controllers.services import order_eligibility as eligibility
from orders_app.controllers.services.order_authority import StaffAuthority
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import OrderAcceptance, OrderQuoteClosure
from orders_app.tests_quote_lifetime import QuoteFixtureMixin
from restaurants_app.controllers import diner_capability
from restaurants_app.controllers.diner_capability import capability_from_table
from restaurants_app.models import RestaurantEmployee, Table


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
