"""
TRIAGE — A DELEGATED READ HANDS OUT LIVE DINER AUTHORITY FOR EVERY TABLE.

**THIS FILE CHANGES NO BEHAVIOUR AND ASSERTS NO FIX.** It is a CHARACTERIZATION
of what the deployed system does today, written so the exposure is measured
rather than described, and so the containment decision (recorded in
`DELEGATED_QR_TRIAGE.md`) is made against facts. Every assertion below passes on
unmodified `main`. If one of them starts FAILING, the exposure has been closed or
moved and this file is the place to say which.

WHAT IT IS. `SETUP_READABLE_RECORDS` admits `tables` to the delegated
restaurant-setup GET, and that read's serializer MINTS a QR credential per table
(`SerializerPublicGetTable.get_qr_credential` -> `issue_qr_credential`). A QR
credential is the SOLE anonymous authority for a table: presenting it to
`orders/journey/table-scan/` mints a diner table session, which is what places
orders. So a delegated administrator holding even the `view` scope — the READ-ONLY
one — receives working anonymous ordering authority for every table in the
restaurant, as an ordinary consequence of opening the tables list.

FOUR PROPERTIES MAKE IT WORTH TRIAGING RATHER THAN NOTING:

  1. It is not an id. The value EXCHANGES for a session (asserted here through
     the real route, not by reading the signer).
  2. `view` is enough. The read-only scope was chosen so a delegate could look
     without acting; this is the one read that hands over the ability to act.
  3. It OUTLIVES the delegation. The credential is verified WITHOUT expiry — it
     is bound to `Table.qr_version`, not to a clock or to the session that
     disclosed it — so ending the delegation, or letting the grant lapse, revokes
     nothing.
  4. Only a QR REGENERATION revokes it, which reprints every physical code on
     that table. The remedy costs the restaurant real work.

WHAT IT IS NOT. It is not a tenant-boundary break: the delegate was granted this
restaurant, and the credential is for that restaurant's tables. It is not a
privilege escalation ACROSS tenants, and it is not a defect in the capability
design — `issue_qr_credential` is doing exactly what the owner-facing read needs
it to do. The question is whether a DELEGATED principal should receive it, and
that is a delegation-scope decision.

**NO DELEGATION SCOPE IS CHANGED HERE, DELIBERATELY.** Narrowing
`SETUP_READABLE_RECORDS`, or teaching the serializer to withhold the field from a
delegated reader, both change what a delegated session can do and both have
consequences for the Admin surfaces that read tables. That belongs in its own
change, with its own review.
"""
from django.core.cache import cache
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from platform_admin_app import delegated_sessions, delegation
from platform_admin_app.configs.delegation_scopes import (
    SCOPE_VIEW, SETUP_READABLE_RECORDS,
)
from platform_admin_app.delegated_middleware import SESSION_HEADER
from restaurants_app.controllers.diner_capability import CREDENTIAL_HEADER
from restaurants_app.models import (
    DiningArea, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

TABLES_URL = '/api/v1/restaurant-setup/tables/'
SCAN_URL = '/api/v1/orders/journey/table-scan/'
END_URL = '/api/v1/delegation/end/'

_SESSION_META = 'HTTP_' + SESSION_HEADER.upper().replace('-', '_')
_CREDENTIAL_META = 'HTTP_' + CREDENTIAL_HEADER.upper().replace('-', '_')


class DelegatedQrDisclosureTriageTests(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = User.objects.create_user(
            first_name='P', last_name='S', email='qr-triage-admin@t.com',
            username='qr-triage-admin', phone_number=None,
            country='Uganda', password='correct-horse-battery', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.owner = User.objects.create_user(
            first_name='O', last_name='W', email='qr-triage-owner@t.com',
            phone_number='256700044901', username='256700044901',
            country='Uganda', password='correct-horse-battery', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Triage R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant)
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )

    def _delegated(self, scope=SCOPE_VIEW):
        raw_code, _grant = delegation.mint_grant(
            administrator=self.admin, admin_session=None,
            restaurant=self.restaurant, scope=scope,
            reason='Diagnosing a reported problem on the live floor.',
        )
        raw_token, _context = delegated_sessions.exchange_code(raw_code)
        return {_SESSION_META: raw_token}

    def _tables_as_delegate(self, headers):
        return self.client.get(TABLES_URL, **headers)

    @staticmethod
    def _rows(response):
        """The table rows out of the restaurant-setup list envelope.

        `Secretary.read()` paginates, so `data` is
        `{'records': [...], 'pagination': {...}}` — NOT a bare list. Stated once
        here so a shape change fails in one place rather than five.
        """
        return response.json()['data']['records']

    # -- 1. the route and the permission ----------------------------------

    def test_the_tables_read_is_on_the_delegated_allowlist(self):
        """Stated from the configuration itself, so this file does not go stale
        quietly if the vocabulary moves."""
        self.assertIn('tables', SETUP_READABLE_RECORDS)

    def test_a_VIEW_scope_delegate_may_read_the_tables_list(self):
        response = self._tables_as_delegate(self._delegated())
        self.assertEqual(response.status_code, 200, response.content)

    # -- 2. the disclosure -------------------------------------------------

    def test_the_read_hands_the_delegate_a_QR_CREDENTIAL_per_table(self):
        response = self._tables_as_delegate(self._delegated())
        rows = self._rows(response)
        self.assertTrue(rows, response.content)
        credentials = [row.get('qr_credential') for row in rows]
        self.assertTrue(
            all(isinstance(c, str) and c for c in credentials),
            'every table row carries a live credential: %r' % (credentials,),
        )

    def test_the_GROUPED_read_hands_them_over_too(self):
        """TWO reads, not one — and this is the one the exposure statement is
        easiest to get wrong about.

        `?grouping` is a DIFFERENT branch with a DIFFERENT builder
        (`controllers/tables.py::get_tables_by_area`, which mints in two places
        of its own for assigned and unassigned tables), but it is the SAME
        route and the SAME `config_detail`, so one allowlist entry admits both.
        Narrowing only the serializer would leave this half wide open.
        """
        response = self.client.get(
            TABLES_URL + '?grouping=area&restaurant=%s' % (self.restaurant.pk,),
            **self._delegated()
        )

        self.assertEqual(response.status_code, 200, response.content)
        minted = [
            table.get('qr_credential')
            for area in response.json()['data']
            for table in area['tables']
        ]
        self.assertTrue(minted, response.content)
        self.assertTrue(all(isinstance(c, str) and c for c in minted), minted)

    # -- 3. THE EXCHANGE — it is authority, not an identifier --------------

    def test_that_credential_MINTS_A_DINER_SESSION(self):
        """The property that makes this worth triaging.

        Asserted through the REAL scan route rather than by inspecting the
        signer: what matters is not that the value is well-formed but that the
        anonymous ordering channel accepts it.
        """
        rows = self._rows(self._tables_as_delegate(self._delegated()))
        credential = rows[0]['qr_credential']

        scanned = self.client.get(SCAN_URL, **{_CREDENTIAL_META: credential})

        self.assertEqual(scanned.status_code, 200, scanned.content)
        minted = scanned.json()['data'].get('session_token')
        self.assertTrue(
            isinstance(minted, str) and minted,
            'the scan minted a diner table session for a credential the '
            'delegate obtained by opening a list',
        )

    # -- 4. it OUTLIVES the delegation ------------------------------------

    def test_the_credential_still_works_after_the_delegation_ENDS(self):
        headers = self._delegated()
        credential = self._rows(
            self._tables_as_delegate(headers))[0]['qr_credential']

        ended = self.client.post(END_URL, **headers)
        self.assertIn(ended.status_code, (200, 204), ended.content)
        # The session really is gone. 401, not 403: the delegated session WAS
        # the request's only authentication, so revoking it leaves an anonymous
        # caller rather than a recognised one who is refused.
        self.assertEqual(self._tables_as_delegate(headers).status_code, 401)

        # The credential is not.
        scanned = self.client.get(SCAN_URL, **{_CREDENTIAL_META: credential})
        self.assertEqual(
            scanned.status_code, 200,
            'ending the delegation revokes nothing about the credential',
        )

    # -- 5. what DOES revoke it -------------------------------------------

    def test_only_a_QR_REGENERATION_revokes_it(self):
        """The remedy, and its cost: `qr_version` is what the credential is
        bound to, so revoking means reprinting every physical code on the
        table."""
        credential = self._rows(
            self._tables_as_delegate(self._delegated()))[0]['qr_credential']
        self.assertEqual(
            self.client.get(SCAN_URL, **{_CREDENTIAL_META: credential})
            .status_code, 200)

        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)

        self.assertEqual(
            self.client.get(SCAN_URL, **{_CREDENTIAL_META: credential})
            .status_code, 404,
            'a regeneration is the only thing that revokes a credential',
        )

    # -- 6. the boundary that is NOT crossed ------------------------------

    def test_it_discloses_nothing_about_ANOTHER_restaurant(self):
        """The control. This is a delegation-scope question, not a tenant
        boundary one: the delegate was granted THIS restaurant, and the read
        stays inside it."""
        other_owner = User.objects.create_user(
            first_name='X', last_name='Y', email='qr-triage-other@t.com',
            phone_number='256700044902', username='256700044902',
            country='Uganda', password='correct-horse-battery', roles=[],
        )
        other = Restaurant.objects.create(
            name='Other R', location='elsewhere', owner=other_owner,
            status=RestaurantStatus_Live,
        )
        other_area = DiningArea.objects.create(name='M', restaurant=other)
        Table.objects.create(
            number=9, restaurant=other, dining_area=other_area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )

        rows = self._rows(self._tables_as_delegate(self._delegated()))

        self.assertTrue(rows)
        for row in rows:
            self.assertNotEqual(str(row.get('restaurant')), str(other.pk))
