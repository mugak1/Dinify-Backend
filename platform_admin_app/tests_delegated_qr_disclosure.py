"""
CONTAINMENT — A DELEGATED READ NO LONGER HANDS OUT DINER ORDERING AUTHORITY.

**THIS FILE USED TO BE A CHARACTERIZATION AND IS NOW A REGRESSION SUITE.** It was
written to MEASURE an exposure rather than fix it, and every assertion in it
passed on unmodified ``main`` by asserting the disclosure. The containment change
inverts the three that asserted it and rebuilds the two whose fixtures depended on
it; the rest are controls and are untouched. The inversion is deliberate and
recorded here rather than done by deleting tests — see ``DELEGATED_QR_TRIAGE.md``.

WHAT WAS EXPOSED. ``SETUP_READABLE_RECORDS`` admits ``tables`` to the delegated
restaurant-setup GET, and that read MINTED a QR credential per table. A QR
credential is the SOLE anonymous authority for a table: presenting it to
``orders/journey/table-scan/`` mints a diner table session, which is what places
orders. So a delegated administrator holding even the ``view`` scope — the
READ-ONLY one — received working anonymous ordering authority for every table in
the restaurant as an ordinary consequence of opening a list.

WHAT IS CONTAINED. The credential is now emitted only to a caller whose ORDINARY,
NON-DELEGATED ``tables`` authority over that table's restaurant has been
POSITIVELY established (``restaurants_app.controllers.qr_disclosure``). A
delegated reader keeps the entire tables view — number, area, capacity, status,
geometry, ``has_qr``, ``qr_mode`` — and loses only the ability to mint diner
authority, which no support work needs. The key is ABSENT, never ``null``, and
the signer is not called at all.

WHAT IS **NOT** CONTAINED, AND MUST NOT BE READ AS IF IT WERE. Containment stops
FUTURE disclosure through these application paths. It does not revoke a credential
already obtained or a diner session already exchanged from one. The two residual-risk
tests below exist to keep that true and visible — and they now obtain their
credential through an ORDINARY AUTHORIZED READ rather than through the delegated
leak, because keeping the leak alive as a test fixture would mean the suite
depended on the thing the change removed.

FOUR PROPERTIES MADE IT WORTH CONTAINING, and three of them still describe the
credential itself (they are facts about the capability design, not about the
exposure):

  1. It is not an id. The value EXCHANGES for a session — asserted here through
     the real route, not by reading the signer.
  2. ``view`` was enough. The read-only scope exists so a delegate can look
     without acting, and this was the one read that handed over the ability to
     act. **Both scopes are now withheld** — they share one read grid, so
     containing only ``view`` would have contained nothing.
  3. It OUTLIVES the delegation. The credential is verified WITHOUT expiry — it
     is bound to ``Table.qr_version``, not to a clock or to the session that
     disclosed it — so ending a delegation revokes nothing. STILL TRUE, and still
     pinned below.
  4. Only a QR REGENERATION revokes it, which reprints every physical code on
     that table. STILL TRUE, and now exercised through the real authorized
     rotation ENDPOINT rather than a direct column write.

IT WAS NEVER A TENANT-BOUNDARY BREAK: the delegate was granted this restaurant and
the credentials were for that restaurant's tables. That control is kept.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from platform_admin_app import delegated_sessions, delegation
from platform_admin_app.configs.delegation_scopes import (
    SCOPE_VIEW, SETUP_READABLE_RECORDS,
)
from platform_admin_app.models import SCOPE_SUPPORT
from platform_admin_app.delegated_middleware import SESSION_HEADER
from restaurants_app.controllers.diner_capability import CREDENTIAL_HEADER
from restaurants_app.models import (
    DiningArea, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

TABLES_URL = '/api/v1/restaurant-setup/tables/'
SCAN_URL = '/api/v1/orders/journey/table-scan/'
END_URL = '/api/v1/delegation/end/'
REGENERATE_URL = '/api/v1/restaurant-setup/table-actions/regenerate-qr/'

_SESSION_META = 'HTTP_' + SESSION_HEADER.upper().replace('-', '_')
_CREDENTIAL_META = 'HTTP_' + CREDENTIAL_HEADER.upper().replace('-', '_')

#: Both builders that can mint. Patched together so "the signer was not called"
#: is a statement about the WHOLE response rather than about one module.
_SIGNER_SITES = (
    'restaurants_app.serializers.issue_qr_credential',
    'restaurants_app.controllers.tables.issue_qr_credential',
)


class DelegatedQrContainmentTests(TestCase):

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

    # -- fixtures ---------------------------------------------------------

    def _delegated(self, scope=SCOPE_VIEW):
        raw_code, _grant = delegation.mint_grant(
            administrator=self.admin, admin_session=None,
            restaurant=self.restaurant, scope=scope,
            reason='Diagnosing a reported problem on the live floor.',
        )
        raw_token, _context = delegated_sessions.exchange_code(raw_code)
        return {_SESSION_META: raw_token}

    def _owner_jwt(self):
        """The ORDINARY authorized principal — the portal's own read path."""
        token = str(RefreshToken.for_user(self.owner).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _tables_as_delegate(self, headers):
        return self.client.get(TABLES_URL, **headers)

    def _credential_through_an_authorized_read(self):
        """A CURRENT credential, obtained the way the owner's portal obtains it.

        The residual-risk properties below are about the CREDENTIAL, not about
        the delegated leak that used to be the convenient way to get one. Taking
        it from the ordinary read is what lets those properties keep being
        asserted without the suite depending on the exposure this change removed.
        """
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.restaurant.id)},
            **self._owner_jwt()
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = self._rows(response)
        self.assertTrue(rows, response.content)
        credential = rows[0].get('qr_credential')
        self.assertTrue(
            isinstance(credential, str) and credential,
            'PREMISE: the ordinary owner read must still carry a live '
            'credential — if this fails the containment has over-reached and '
            'every assertion below is meaningless',
        )
        return credential

    @staticmethod
    def _rows(response):
        """The table rows out of the restaurant-setup list envelope.

        ``Secretary.read()`` paginates, so ``data`` is
        ``{'records': [...], 'pagination': {...}}`` — NOT a bare list. Stated once
        here so a shape change fails in one place rather than five.
        """
        return response.json()['data']['records']

    # -- 1. the route and the permission ----------------------------------

    def test_the_tables_read_is_on_the_delegated_allowlist(self):
        """Stated from the configuration itself, so this file does not go stale
        quietly if the vocabulary moves.

        UNCHANGED BY CONTAINMENT, and deliberately so: the fix withholds a FIELD,
        it does not remove the tables view from delegated access.
        """
        self.assertIn('tables', SETUP_READABLE_RECORDS)

    def test_a_VIEW_scope_delegate_may_read_the_tables_list(self):
        """Also unchanged. A delegate can still open the list — that is the
        point of containing the field rather than the route."""
        response = self._tables_as_delegate(self._delegated())
        self.assertEqual(response.status_code, 200, response.content)

    # -- 2. the containment (was: the disclosure) -------------------------

    def test_the_read_WITHHOLDS_the_QR_CREDENTIAL(self):
        """INVERTED. This asserted the disclosure; it now asserts its absence.

        The key is ABSENT, not ``null`` and not empty: a key present and empty and
        a key absent are different facts to a client, and the contract is absent.
        The rest of the row is untouched — this is field-level containment, not
        removal of the table view.
        """
        response = self._tables_as_delegate(self._delegated())
        rows = self._rows(response)
        self.assertTrue(rows, response.content)
        for row in rows:
            self.assertNotIn('qr_credential', row, row)
        self.assertNotIn('qr_credential', response.content.decode())
        # The view itself survives.
        self.assertEqual(rows[0]['number'], 1)
        self.assertTrue(rows[0]['has_qr'])
        self.assertEqual(rows[0]['qr_mode'], 'order_pay')

    def test_the_GROUPED_read_WITHHOLDS_them_too(self):
        """INVERTED. TWO reads, not one — and this is the one the containment is
        easiest to get wrong about.

        ``?grouping`` is a DIFFERENT branch with a DIFFERENT builder
        (``controllers/tables.py``, which minted in two places of its own for
        assigned and unassigned tables), but it is the SAME route and the SAME
        ``config_detail``, so one allowlist entry admits both. Containing only the
        serializer would leave this half wide open.
        """
        Table.objects.create(
            number=2, restaurant=self.restaurant, dining_area=None,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        response = self.client.get(
            TABLES_URL + '?grouping=area&restaurant=%s' % (self.restaurant.pk,),
            **self._delegated()
        )

        self.assertEqual(response.status_code, 200, response.content)
        groups = response.json()['data']
        rows = [table for area in groups for table in area['tables']]
        # PREMISE: both branches are actually populated, so the assertions below
        # cannot pass vacuously on an empty list.
        self.assertEqual(len(rows), 2, groups)
        self.assertEqual(
            {(area.get('dining_area') or {}).get('name') for area in groups},
            {'Main', 'Not Assigned'},
            'both the assigned and the unassigned branch must be exercised',
        )
        for row in rows:
            self.assertNotIn('qr_credential', row, row)
        self.assertNotIn('qr_credential', response.content.decode())

    def test_the_SUPPORT_scope_is_withheld_as_well(self):
        """NEW. ``view`` and ``support`` share ONE read grid (`_scope_grid` is
        called twice), so containing only the scope the triage happened to
        measure would have contained nothing."""
        response = self._tables_as_delegate(self._delegated(SCOPE_SUPPORT))
        self.assertEqual(response.status_code, 200, response.content)
        rows = self._rows(response)
        self.assertTrue(rows, response.content)
        for row in rows:
            self.assertNotIn('qr_credential', row, row)

    def test_the_SIGNER_IS_NEVER_CALLED_for_a_delegated_read(self):
        """NEW, and the reason the containment removes the field BEFORE
        evaluation rather than stripping it afterwards.

        Signing and then dropping the value leaves a live bearer capability in
        memory for a logger, an exception repr, or the next person who adds a
        ``to_representation`` override above the strip. Withholding has to mean
        the credential was never minted.
        """
        headers = self._delegated()
        with mock.patch(_SIGNER_SITES[0]) as flat_signer, \
                mock.patch(_SIGNER_SITES[1]) as grouped_signer:
            flat = self._tables_as_delegate(headers)
            grouped = self.client.get(
                TABLES_URL + '?grouping=area&restaurant=%s'
                % (self.restaurant.pk,), **headers
            )
        self.assertEqual(flat.status_code, 200)
        self.assertEqual(grouped.status_code, 200)
        self.assertEqual(flat_signer.call_count, 0)
        self.assertEqual(grouped_signer.call_count, 0)

    # -- 3. THE EXCHANGE — it is authority, not an identifier --------------

    def test_a_credential_MINTS_A_DINER_SESSION(self):
        """The property that made this worth containing, restated about the
        CREDENTIAL rather than about the delegated read.

        Asserted through the REAL scan route rather than by inspecting the
        signer: what matters is not that the value is well-formed but that the
        anonymous ordering channel accepts it. The credential now comes from an
        ORDINARY authorized read — the delegated one no longer supplies one.
        """
        credential = self._credential_through_an_authorized_read()

        scanned = self.client.get(SCAN_URL, **{_CREDENTIAL_META: credential})

        self.assertEqual(scanned.status_code, 200, scanned.content)
        minted = scanned.json()['data'].get('session_token')
        self.assertTrue(
            isinstance(minted, str) and minted,
            'the scan minted a diner table session',
        )
        # ...and it is bound to THIS table, not merely well-formed.
        self.assertEqual(scanned.json()['data'].get('number'), self.table.number)

    # -- 4. it OUTLIVES the delegation ------------------------------------

    def test_ending_a_delegation_revokes_NOTHING_about_a_held_credential(self):
        """REBUILT FIXTURE, SAME PROPERTY. This used to obtain its credential
        from the delegated read; that read no longer supplies one, so it takes an
        ordinary authorized one instead.

        The property is unchanged and is the RESIDUAL RISK this containment does
        not address: a credential already obtained — by any means, including
        before this change shipped — is not revoked by the delegation ending.
        """
        credential = self._credential_through_an_authorized_read()
        session = self.client.get(
            SCAN_URL, **{_CREDENTIAL_META: credential}
        ).json()['data']['session_token']

        headers = self._delegated()
        self.assertEqual(self._tables_as_delegate(headers).status_code, 200)

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
            'ending a delegation revokes nothing about a credential already held',
        )
        self.assertTrue(session)

    # -- 5. what DOES revoke it -------------------------------------------

    def test_only_a_QR_REGENERATION_revokes_it(self):
        """REBUILT FIXTURE AND STRENGTHENED. The remedy, and its cost:
        ``qr_version`` is what the credential is bound to, so revoking means
        reprinting every physical code on the table.

        It now drives the REAL authorized rotation ENDPOINT rather than writing
        the column directly. A direct ``update()`` proves the verifier re-checks
        the generation; it does not prove the operator has a working way to
        perform the revocation, which is the whole remedy. Both halves are
        asserted: the old generation dies and the new one works.
        """
        credential = self._credential_through_an_authorized_read()
        session = self.client.get(
            SCAN_URL, **{_CREDENTIAL_META: credential}
        ).json()['data']['session_token']
        self.assertTrue(session)

        rotated = self.client.post(
            REGENERATE_URL, data={'table_id': str(self.table.pk)},
            content_type='application/json', **self._owner_jwt()
        )
        self.assertEqual(rotated.status_code, 200, rotated.content)

        self.assertEqual(
            self.client.get(SCAN_URL, **{_CREDENTIAL_META: credential})
            .status_code, 404,
            'a regeneration is the only thing that revokes a credential',
        )

        # The REPLACEMENT the operator was handed works — otherwise the remedy
        # would destroy the printed codes and leave nothing to reprint.
        replacement = rotated.json()['data'].get('qr_credential')
        self.assertTrue(
            isinstance(replacement, str) and replacement, rotated.content)
        self.assertEqual(
            self.client.get(SCAN_URL, **{_CREDENTIAL_META: replacement})
            .status_code, 200)

    def test_a_DELEGATED_caller_cannot_regenerate_and_is_handed_nothing(self):
        """NEW. The rotation route is off ``ALLOWED_ROUTES``, so a delegated
        session is refused before dispatch — no generation moves and no
        credential is returned."""
        before = Table.objects.get(pk=self.table.pk).qr_version
        refused = self.client.post(
            REGENERATE_URL, data={'table_id': str(self.table.pk)},
            content_type='application/json', **self._delegated(SCOPE_SUPPORT)
        )
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertNotIn('qr_credential', refused.content.decode())
        self.assertEqual(
            Table.objects.get(pk=self.table.pk).qr_version, before,
            'a refused rotation must not move the generation',
        )

    # -- 6. the boundary that is NOT crossed ------------------------------

    def test_it_discloses_nothing_about_ANOTHER_restaurant(self):
        """The control. This was a delegation-scope question, not a tenant
        boundary one: the delegate was granted THIS restaurant, and the read
        stays inside it. UNCHANGED."""
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
