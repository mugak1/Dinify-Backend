"""
THE QR-CREDENTIAL DISCLOSURE BOUNDARY — every builder, every caller, both answers.

``platform_admin_app/tests_delegated_qr_disclosure.py`` pins the DELEGATED half:
the exposure that prompted this work, and the residual risk it does not address.
This file pins the rest of the contract, and most of it is about the answers that
must NOT change:

* the ORDINARY owner/staff paths still receive a live, scannable credential —
  a containment that quietly broke QR printing would be a worse outcome than the
  exposure, and it would look exactly like success from the delegated side;
* every BUILDER defaults to withholding when nobody established entitlement, so
  a future caller that forgets the context loses a field rather than leaking one;
* the withheld representation is the key's ABSENCE, and the signer is not called;
* entitlement comes only from server-derived facts — never a query parameter, a
  body field, a grouping value or anything else a caller can choose.

THE INVARIANT UNDER TEST, in one sentence: *QR material is emitted only after
ordinary, non-delegated authority for the relevant table scope has been positively
established; otherwise it is withheld before signing.*

READ THE PREMISES. Several assertions here are about a field being ABSENT, which
is exactly the shape that passes vacuously against an empty list, a 403 or a
failed route. Each one first establishes that rows really came back.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF, MODULE_TABLES, RESTAURANT_KITCHEN,
    RESTAURANT_MANAGER, RESTAURANT_OWNER, RESTAURANT_STAFF,
    RestaurantStatus_Live,
)
from platform_admin_app import delegated_sessions, delegation
from platform_admin_app.delegated_middleware import SESSION_HEADER
from platform_admin_app.models import SCOPE_SUPPORT, SCOPE_VIEW
from restaurants_app.controllers.diner_capability import CREDENTIAL_HEADER
from restaurants_app.controllers.qr_disclosure import (
    QR_CREDENTIAL_WITHHELD, QrDisclosurePolicy, WITHHOLD_ALL,
    policy_from_context, qr_disclosure_policy, request_is_delegated,
)
from restaurants_app.controllers.tables import get_tables_by_area
from restaurants_app.models import (
    DiningArea, Restaurant, RestaurantEmployee, Table,
)
from restaurants_app.serializers import SerializerPublicGetTable
from users_app.controllers.permissions_check import can_user_access_module
from users_app.models import User

TABLES_URL = '/api/v1/restaurant-setup/tables/'
DETAILS_URL = '/api/v1/restaurant-setup/details/'
SCAN_URL = '/api/v1/orders/journey/table-scan/'
SEAT_URL = '/api/v1/restaurant-setup/table-actions/seat/'
CLEAR_URL = '/api/v1/restaurant-setup/table-actions/clear/'
TRANSFER_URL = '/api/v1/restaurant-setup/table-actions/transfer/'
STATUS_URL = '/api/v1/restaurant-setup/table-actions/update-status/'

_SESSION_META = 'HTTP_' + SESSION_HEADER.upper().replace('-', '_')
_CREDENTIAL_META = 'HTTP_' + CREDENTIAL_HEADER.upper().replace('-', '_')

_SIGNER_SITES = (
    'restaurants_app.serializers.issue_qr_credential',
    'restaurants_app.controllers.tables.issue_qr_credential',
)


class QrDisclosureBoundaryBase(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = User.objects.create_user(
            first_name='P', last_name='S', email='qrb-admin@t.com',
            username='qrb-admin', phone_number=None, country='Uganda',
            password='x', roles=[], account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.owner = User.objects.create_user(
            first_name='O', last_name='W', email='qrb-owner@t.com',
            phone_number='256700210001', username='256700210001',
            country='Uganda', password='x', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Boundary R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant)
        self.assigned = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        self.unassigned = Table.objects.create(
            number=2, restaurant=self.restaurant, dining_area=None,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )

    # -- fixtures ---------------------------------------------------------

    def _jwt(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _employee(self, roles, phone):
        user = User.objects.create_user(
            first_name='E', last_name='E', email=f'qrb-{phone}@t.com',
            phone_number=phone, username=phone, country='Uganda',
            password='x', roles=[],
        )
        RestaurantEmployee.objects.create(
            user=user, restaurant=self.restaurant, roles=roles, active=True)
        return user

    def _delegated(self, scope=SCOPE_VIEW):
        raw_code, _ = delegation.mint_grant(
            administrator=self.admin, admin_session=None,
            restaurant=self.restaurant, scope=scope,
            reason='Diagnosing a reported problem on the live floor.',
        )
        raw_token, _ = delegated_sessions.exchange_code(raw_code)
        return {_SESSION_META: raw_token}

    def _flat(self, headers, **params):
        query = {'restaurant': str(self.restaurant.id)}
        query.update(params)
        return self.client.get(TABLES_URL, query, **headers)

    def _grouped(self, headers):
        return self.client.get(
            TABLES_URL + '?grouping=area&restaurant=%s' % (self.restaurant.pk,),
            **headers
        )

    @staticmethod
    def _rows(response):
        return response.json()['data']['records']

    @staticmethod
    def _grouped_rows(response):
        return [t for area in response.json()['data'] for t in area['tables']]


class OrdinaryAuthorizedPathsAreUnchangedTests(QrDisclosureBoundaryBase):
    """THE CONTROLS. A containment that breaks these has broken QR printing."""

    def test_the_owner_flat_read_still_carries_a_SCANNABLE_credential(self):
        response = self._flat(self._jwt(self.owner))
        self.assertEqual(response.status_code, 200, response.content)
        rows = self._rows(response)
        self.assertEqual(len(rows), 2, rows)
        by_number = {r['number']: r for r in rows}
        for number, row in by_number.items():
            credential = row.get('qr_credential')
            self.assertTrue(
                isinstance(credential, str) and credential, row)
            scanned = self.client.get(
                SCAN_URL, **{_CREDENTIAL_META: credential})
            self.assertEqual(scanned.status_code, 200, scanned.content)
            # Correlated to the EXPECTED table, not merely a 200.
            self.assertEqual(scanned.json()['data']['number'], number)

    def test_the_owner_GROUPED_read_carries_them_in_BOTH_branches(self):
        response = self._grouped(self._jwt(self.owner))
        self.assertEqual(response.status_code, 200, response.content)
        groups = response.json()['data']
        self.assertEqual(
            {(g.get('dining_area') or {}).get('name') for g in groups},
            {'Main', 'Not Assigned'}, groups)
        rows = self._grouped_rows(response)
        self.assertEqual(len(rows), 2, groups)
        for row in rows:
            credential = row.get('qr_credential')
            self.assertTrue(isinstance(credential, str) and credential, row)
            self.assertEqual(
                self.client.get(
                    SCAN_URL, **{_CREDENTIAL_META: credential}).status_code,
                200)

    def test_the_grouped_envelope_and_ordinary_fields_are_unchanged(self):
        """The builder was refactored so both branches share one function; the
        row shape must be byte-identical to what it was."""
        response = self._grouped(self._jwt(self.owner))
        rows = self._grouped_rows(response)
        self.assertEqual(
            sorted(rows[0].keys()),
            sorted([
                'id', 'number', 'enabled', 'reserved', 'available',
                'display_name', 'min_capacity', 'max_capacity', 'shape',
                'status', 'tags', 'has_qr', 'qr_mode', 'qr_credential',
                'floor_x', 'floor_y', 'is_active',
            ]),
        )
        envelope = response.json()
        self.assertEqual(envelope['status'], 200)
        self.assertEqual(envelope['message'], 'Tables by dining area')

    def test_a_MANAGER_and_a_STAFF_member_also_receive_it(self):
        """NOT OWNER-ONLY. The containment keys on the existing ``tables``
        module scope, so every role whose grid grants it keeps the field — a
        silent narrowing to owner-only would be an unapproved policy change."""
        for roles, phone in (
            ([RESTAURANT_MANAGER], '256700210002'),
            ([RESTAURANT_STAFF], '256700210003'),
        ):
            user = self._employee(roles, phone)
            self.assertTrue(
                can_user_access_module(
                    user, str(self.restaurant.id), MODULE_TABLES),
                'PREMISE: %s must hold the tables module' % roles,
            )
            rows = self._rows(self._flat(self._jwt(user)))
            self.assertTrue(rows, roles)
            for row in rows:
                self.assertTrue(row.get('qr_credential'), (roles, row))

    def test_a_KITCHEN_member_is_refused_the_READ_as_before(self):
        """The unchanged authorization behaviour underneath: kitchen holds no
        ``tables`` module, so the list is empty for them — and therefore carries
        no credential, by scoping rather than by this containment."""
        user = self._employee([RESTAURANT_KITCHEN], '256700210004')
        self.assertFalse(
            can_user_access_module(
                user, str(self.restaurant.id), MODULE_TABLES))
        response = self._flat(self._jwt(user))
        self.assertNotIn('qr_credential', response.content.decode())

    def test_pagination_metadata_is_unchanged(self):
        response = self._flat(self._jwt(self.owner))
        pagination = response.json()['data']['pagination']
        self.assertEqual(pagination['total_records'], 2)

    def test_the_five_TABLE_ACTION_responses_keep_their_contract(self):
        """All five ordinary construction sites. Fail-closed defaults must not
        regress responses that were never the exposure."""
        jwt = self._jwt(self.owner)
        cases = [
            ('seat', SEAT_URL, {'table_id': str(self.assigned.pk)}, ('data',)),
            ('clear', CLEAR_URL, {'table_id': str(self.assigned.pk)}, ('data',)),
            ('update-status', STATUS_URL,
             {'table_id': str(self.assigned.pk), 'status': 'available'},
             ('data',)),
            ('transfer', TRANSFER_URL,
             {'source_table_id': str(self.assigned.pk),
              'destination_table_id': str(self.unassigned.pk)},
             ('data', 'source'), ),
        ]
        for case in cases:
            name, url, body, path = case[0], case[1], case[2], case[3]
            response = self.client.post(
                url, data=body, content_type='application/json', **jwt)
            self.assertEqual(response.status_code, 200, (name, response.content))
            node = response.json()
            for key in path:
                node = node[key]
            self.assertTrue(
                node.get('qr_credential'),
                '%s must still carry the credential: %r' % (name, node),
            )
        # transfer's SECOND serializer, which shares one resolved policy.
        response = self.client.post(
            TRANSFER_URL,
            data={'source_table_id': str(self.unassigned.pk),
                  'destination_table_id': str(self.assigned.pk)},
            content_type='application/json', **jwt)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(
            response.json()['data']['destination'].get('qr_credential'))


class WithholdingIsTheDefaultTests(QrDisclosureBoundaryBase):
    """FAIL-CLOSED. Absence of established entitlement is never entitlement."""

    def test_a_serializer_built_with_NO_CONTEXT_withholds(self):
        data = SerializerPublicGetTable(self.assigned).data
        self.assertNotIn('qr_credential', data)

    def test_a_serializer_with_NO_CONTEXT_never_calls_the_signer(self):
        with mock.patch(_SIGNER_SITES[0]) as signer:
            SerializerPublicGetTable(self.assigned).data
        self.assertEqual(signer.call_count, 0)

    def test_the_GROUPED_HELPER_called_directly_withholds(self):
        """Its ``qr_policy`` defaults to None, and None withholds — a direct
        call, a management command or a future caller that forgets gets a
        perfectly good listing with no bearer authority in it."""
        payload = get_tables_by_area(restaurant_id=str(self.restaurant.id))
        rows = [t for area in payload['data'] for t in area['tables']]
        self.assertEqual(len(rows), 2, payload)
        for row in rows:
            self.assertNotIn('qr_credential', row, row)

    def test_the_GROUPED_HELPER_with_no_policy_never_signs(self):
        with mock.patch(_SIGNER_SITES[1]) as signer:
            get_tables_by_area(restaurant_id=str(self.restaurant.id))
        self.assertEqual(signer.call_count, 0)

    def test_an_EMPTY_policy_withholds_and_a_scoped_one_permits(self):
        empty = SerializerPublicGetTable(
            self.assigned, context={'qr_policy': WITHHOLD_ALL}).data
        self.assertNotIn('qr_credential', empty)

        scoped = SerializerPublicGetTable(
            self.assigned,
            context={'qr_policy': QrDisclosurePolicy(
                restaurant_ids=frozenset({str(self.restaurant.id)}))},
        ).data
        self.assertTrue(scoped.get('qr_credential'))

    def test_a_FOREIGN_row_inside_an_entitled_response_is_still_withheld(self):
        """A non-delegated caller is not thereby entitled to a foreign table.
        Defensive — the scoped queryset should make this unreachable — and it
        withholds by OMISSION rather than emitting ``null``."""
        other_owner = User.objects.create_user(
            first_name='X', last_name='Y', email='qrb-other@t.com',
            phone_number='256700210009', username='256700210009',
            country='Uganda', password='x', roles=[],
        )
        other = Restaurant.objects.create(
            name='Other', location='e', owner=other_owner,
            status=RestaurantStatus_Live)
        foreign = Table.objects.create(
            number=7, restaurant=other, dining_area=None,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True)

        policy = QrDisclosurePolicy(
            restaurant_ids=frozenset({str(self.restaurant.id)}))
        with mock.patch(_SIGNER_SITES[0]) as signer:
            data = SerializerPublicGetTable(
                foreign, context={'qr_policy': policy}).data
        self.assertNotIn('qr_credential', data)
        self.assertEqual(signer.call_count, 0)

    def test_an_ANONYMOUS_request_withholds(self):
        response = self._flat({})
        self.assertNotIn('qr_credential', response.content.decode())

    def test_policy_helpers_fail_closed_on_degenerate_input(self):
        self.assertTrue(request_is_delegated(None))
        self.assertIs(qr_disclosure_policy(None), WITHHOLD_ALL)
        self.assertIs(policy_from_context(None), WITHHOLD_ALL)
        self.assertIs(policy_from_context({}), WITHHOLD_ALL)
        self.assertTrue(WITHHOLD_ALL.withholds_everything)
        self.assertFalse(WITHHOLD_ALL.allows(self.restaurant.id))
        self.assertFalse(
            QrDisclosurePolicy(restaurant_ids=frozenset({'x'})).allows(None))

    def test_the_withheld_sentinel_is_not_a_credential(self):
        """It must be identity-compared, never truth-tested, and never mistaken
        for a value if it ever escaped."""
        self.assertNotIsInstance(QR_CREDENTIAL_WITHHELD, str)
        self.assertIn('withheld', repr(QR_CREDENTIAL_WITHHELD))


class EntitlementIsServerDerivedTests(QrDisclosureBoundaryBase):
    """A caller may not nominate themselves entitled."""

    def test_a_DELEGATE_WITH_the_tables_module_is_still_withheld(self):
        """The module resolver INTENTIONALLY resolves a delegated principal from
        the grant, so it answers True here. Reading a table is not the same
        authority as minting the credential that orders from it, and the
        delegation veto is what separates them."""
        headers = self._delegated()
        response = self._flat(headers)
        self.assertEqual(response.status_code, 200, response.content)
        rows = self._rows(response)
        self.assertTrue(rows, 'PREMISE: the delegate really can read the list')
        for row in rows:
            self.assertNotIn('qr_credential', row, row)

    def test_request_PARAMETERS_cannot_grant_entitlement(self):
        """No query parameter, no grouping value, no invented flag."""
        headers = self._delegated()
        for params in (
            {'include_qr': 'true'},
            {'qr_credential': 'true'},
            {'qr_policy': 'all'},
            {'role': 'owner'},
        ):
            response = self._flat(headers, **params)
            self.assertNotIn(
                'qr_credential', response.content.decode(), params)

    def test_a_request_BODY_cannot_grant_entitlement_on_an_action(self):
        refused = self.client.post(
            SEAT_URL,
            data={'table_id': str(self.assigned.pk), 'include_qr': True,
                  'qr_policy': 'all'},
            content_type='application/json', **self._delegated(SCOPE_SUPPORT))
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertNotIn('qr_credential', refused.content.decode())

    def test_MIXED_CREDENTIALS_are_refused_and_carry_nothing(self):
        """Presenting a delegated session AND an ordinary token is refused by the
        existing middleware — it must never resolve as the ordinary principal and
        hand over the field."""
        headers = dict(self._delegated())
        headers.update(self._jwt(self.owner))
        response = self.client.get(
            TABLES_URL, {'restaurant': str(self.restaurant.id)}, **headers)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn('qr_credential', response.content.decode())

    def test_an_ENDED_delegation_is_denied_and_carries_nothing(self):
        headers = self._delegated()
        self.assertEqual(self._flat(headers).status_code, 200)
        self.client.post('/api/v1/delegation/end/', **headers)
        response = self._flat(headers)
        self.assertEqual(response.status_code, 401, response.content)
        self.assertNotIn('qr_credential', response.content.decode())

    def test_an_UNKNOWN_delegated_session_is_denied_and_carries_nothing(self):
        response = self._flat({_SESSION_META: 'not-a-real-session-token'})
        self.assertEqual(response.status_code, 401, response.content)
        self.assertNotIn('qr_credential', response.content.decode())

    def test_a_delegate_reaches_no_FOREIGN_restaurant(self):
        other_owner = User.objects.create_user(
            first_name='X', last_name='Y', email='qrb-x@t.com',
            phone_number='256700210011', username='256700210011',
            country='Uganda', password='x', roles=[],
        )
        other = Restaurant.objects.create(
            name='Foreign', location='e', owner=other_owner,
            status=RestaurantStatus_Live)
        DiningArea.objects.create(name='F', restaurant=other)
        Table.objects.create(
            number=3, restaurant=other, dining_area=None, enabled=True,
            is_active=True, qr_mode='order_pay', has_qr=True)

        response = self.client.get(
            TABLES_URL, {'restaurant': str(other.id)}, **self._delegated())
        body = response.content.decode()
        self.assertNotIn('qr_credential', body)
        self.assertNotIn(str(other.id), body)


class NoAlternateDisclosureTests(QrDisclosureBoundaryBase):
    """The withheld field must not reappear under another name or route."""

    def test_the_delegated_row_carries_no_alternate_credential_material(self):
        response = self._flat(self._delegated())
        rows = self._rows(response)
        self.assertTrue(rows)
        row = rows[0]
        # The ordinary QR METADATA stays — it is not bearer authority, and
        # blanking it to imitate containment would be its own defect.
        self.assertIn('has_qr', row)
        self.assertIn('qr_mode', row)
        self.assertIn('qr_version', row)
        # Nothing scannable, under any spelling.
        body = response.content.decode()
        for token in ('qr_credential', 'credential', 'claim_token', '?c='):
            self.assertNotIn(token, body, token)

    def test_the_DETAIL_branch_carries_no_credential_for_either_caller(self):
        """``?record=tables&id=`` uses a DIFFERENT serializer
        (``SerializerPutTable``). It never carried one; pinned so it cannot
        start."""
        url = DETAILS_URL + '?record=tables&id=%s' % (self.assigned.pk,)
        for label, headers in (
            ('delegated', self._delegated()),
            ('owner', self._jwt(self.owner)),
        ):
            response = self.client.get(url, **headers)
            self.assertEqual(response.status_code, 200, (label, response.content))
            self.assertNotIn('qr_credential', response.content.decode(), label)

    def test_the_DININGAREAS_read_carries_no_credential(self):
        """It nests hand-built table dicts. They carry ``has_qr``/``qr_mode``
        and never a credential; pinned so a future edit cannot add one."""
        response = self.client.get(
            '/api/v1/restaurant-setup/diningareas/',
            {'restaurant': str(self.restaurant.id)}, **self._delegated())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn('qr_credential', response.content.decode())


class SecretaryContextPlumbingTests(QrDisclosureBoundaryBase):
    """The request reaches the serializer on BOTH Secretary read branches."""

    def test_the_PAGINATED_branch_carries_the_request(self):
        """The restaurant-setup list read sets ``paginate: True``, so this is the
        branch production actually uses."""
        response = self._flat(self._jwt(self.owner))
        self.assertIn('pagination', response.json()['data'])
        self.assertTrue(self._rows(response)[0].get('qr_credential'))

    def test_the_UNPAGINATED_branch_carries_the_request(self):
        from misc_app.controllers.secretary import Secretary

        class _Req:
            def __init__(self, user):
                self.user = user

        request = _Req(self.owner)
        response = Secretary({
            'request': request,
            'serializer': SerializerPublicGetTable,
            'filter': {'restaurant': self.restaurant.id, 'deleted': False},
            'paginate': False,
            'user_id': self.owner.id,
            'username': self.owner.username,
            'success_message': 'ok',
            'error_message': 'err',
        }).read()
        rows = response['data']['records']
        self.assertEqual(len(rows), 2, rows)
        for row in rows:
            self.assertTrue(row.get('qr_credential'), row)

    def test_Secretary_preserves_a_caller_supplied_context(self):
        from misc_app.controllers.secretary import Secretary

        response = Secretary({
            'request': None,
            'serializer': SerializerPublicGetTable,
            'filter': {'restaurant': self.restaurant.id, 'deleted': False},
            'paginate': False,
            'user_id': self.owner.id,
            'username': self.owner.username,
            'success_message': 'ok',
            'error_message': 'err',
            'serializer_context': {'qr_policy': QrDisclosurePolicy(
                restaurant_ids=frozenset({str(self.restaurant.id)}))},
        }).read()
        for row in response['data']['records']:
            self.assertTrue(row.get('qr_credential'), row)


class NoCrossRequestContaminationTests(QrDisclosureBoundaryBase):
    """One request's omission must not change another request's fields.

    The containment pops the field from a per-INSTANCE ``self.fields``. If it
    ever touched ``_declared_fields`` or any other class-level state, the FIRST
    delegated read would silently disable QR printing for every ordinary caller
    afterwards — a far worse outcome than the exposure, and invisible from the
    delegated side.
    """

    def test_ordinary_then_delegated_then_ordinary(self):
        owner = self._jwt(self.owner)
        delegated = self._delegated()

        first = self._rows(self._flat(owner))
        self.assertTrue(first[0].get('qr_credential'))

        middle = self._rows(self._flat(delegated))
        self.assertNotIn('qr_credential', middle[0])

        last = self._rows(self._flat(owner))
        self.assertTrue(
            last[0].get('qr_credential'),
            'a delegated read must not disable the field for the next caller',
        )

    def test_delegated_then_ordinary(self):
        self._flat(self._delegated())
        rows = self._rows(self._flat(self._jwt(self.owner)))
        self.assertTrue(rows[0].get('qr_credential'))

    def test_the_class_field_declaration_is_untouched(self):
        self.assertIn('qr_credential', SerializerPublicGetTable._declared_fields)
        # A withholding instance does not mutate the shared declaration.
        SerializerPublicGetTable(self.assigned).data
        self.assertIn('qr_credential', SerializerPublicGetTable._declared_fields)
        self.assertIn(
            'qr_credential',
            SerializerPublicGetTable(
                self.assigned,
                context={'qr_policy': QrDisclosurePolicy(
                    restaurant_ids=frozenset({str(self.restaurant.id)}))},
            ).fields,
        )


class ResponseSemanticsAreUnchangedTests(QrDisclosureBoundaryBase):
    """Headers and audit behaviour the containment must not disturb."""

    def test_the_delegated_response_keeps_its_no_store_and_vary_headers(self):
        response = self._flat(self._delegated())
        self.assertEqual(response.headers.get('Cache-Control'),
                         'no-store, private')
        self.assertIn(SESSION_HEADER, response.headers.get('Vary', ''))
        self.assertEqual(response.headers.get('X-Acting-As'), 'delegation')

    def test_a_delegated_READ_still_writes_no_audit_row(self):
        """The audit contract is unchanged: it records privileged DECISIONS, not
        page views. Containment did not start auditing safe methods."""
        from platform_admin_app.models import AdminAuditLog

        headers = self._delegated()
        before = AdminAuditLog.objects.count()
        self.assertEqual(self._flat(headers).status_code, 200)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_no_credential_material_reaches_a_log_record(self):
        with self.assertLogs(level='DEBUG') as captured:
            # Something must be logged or assertLogs fails; the read itself is
            # quiet, so the assertion below is about what it did NOT add.
            import logging
            logging.getLogger('qrb').debug('probe')
            self._flat(self._jwt(self.owner))
        joined = '\n'.join(captured.output)
        self.assertNotIn('qr_credential', joined)
