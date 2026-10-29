"""
The in-app notifications store (SHELL-FINISH-00, PR 4).

Every producer test drives the REAL triggering endpoint (review submission, the
kitchen stock toggle, the restaurants PUT) and the REAL inbox routes, so the
claims are about the deployed path rather than a helper. ``TestCase`` wraps each
test in a transaction, so ``on_commit`` callbacks run only when a test asks for
them with ``captureOnCommitCallbacks(execute=True)``; that is what lets these
tests show a producer fires AFTER commit and not before.
"""
import json
from datetime import timedelta
from unittest import mock

from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    MODULE_REVIEWS, MODULE_TABLES, OrderStatus_Served, RESTAURANT_MANAGER,
    RESTAURANT_OWNER, RESTAURANT_STAFF, RestaurantStatus_Live,
)
from notifications_app import producers
from notifications_app.models import (
    NotificationKind, NotificationRead, RestaurantNotification,
)
from orders_app.models import Order
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee,
    RestaurantRolePermission, Table,
)
from reviews_app.models import Review
from users_app.models import User

INBOX = '/api/v1/notifications/inbox/'
COUNT = '/api/v1/notifications/inbox/unread-count/'
READ_ALL = '/api/v1/notifications/inbox/read-all/'
SUBMIT_REVIEW = '/api/v1/reviews/submit/'
RESTAURANTS_PUT = '/api/v1/restaurant-setup/restaurants/'


def _read_url(pk):
    return f'/api/v1/notifications/inbox/{pk}/read/'


def _stock_url(pk):
    return f'/api/v1/kitchen/menu-items/{pk}/stock/'


def _user(phone, first='Test'):
    return User.objects.create_user(
        first_name=first, last_name='User', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class InboxTestBase(TestCase):
    """Two live restaurants. A has an owner, a manager and a front-of-house staff
    member; B has its own owner. One menu item at A to sell out."""

    def setUp(self):
        self.owner_a = _user('256700100001', 'Amina')
        self.restaurant_a = Restaurant.objects.create(
            name='Restaurant A', location='loc-a', status=RestaurantStatus_Live,
            owner=self.owner_a, accepting_orders=True,
        )
        self.manager_a = _user('256700100002', 'Moses')
        self.staff_a = _user('256700100003', 'Sarah')
        for user, roles in ((self.owner_a, [RESTAURANT_OWNER]),
                            (self.manager_a, [RESTAURANT_MANAGER]),
                            (self.staff_a, [RESTAURANT_STAFF])):
            RestaurantEmployee.objects.create(user=user, restaurant=self.restaurant_a, roles=roles)
        self.table_a = Table.objects.create(number=1, restaurant=self.restaurant_a)

        self.owner_b = _user('256700100011', 'Brian')
        self.restaurant_b = Restaurant.objects.create(
            name='Restaurant B', location='loc-b', status=RestaurantStatus_Live,
            owner=self.owner_b, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b, roles=[RESTAURANT_OWNER])
        self.outsider = _user('256700100021', 'Olive')

        section = MenuSection.objects.create(restaurant=self.restaurant_a, name='Mains')
        self.item = MenuItem.objects.create(
            section=section, name='Rolex', primary_price=5000, in_stock=True,
        )

    # --- helpers -------------------------------------------------------
    def auth(self, user):
        return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}

    def inbox(self, user, restaurant):
        return self.client.get(INBOX, {'restaurant': str(restaurant.pk)}, **self.auth(user))

    def count(self, user, restaurant):
        resp = self.client.get(COUNT, {'restaurant': str(restaurant.pk)}, **self.auth(user))
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()['data']['unread_count']

    def kinds(self, user, restaurant):
        resp = self.inbox(user, restaurant)
        self.assertEqual(resp.status_code, 200, resp.content)
        return sorted(r['kind'] for r in resp.json()['data']['results'])

    def sell_out(self, user, in_stock=False):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(
                _stock_url(self.item.pk), data=json.dumps({'in_stock': in_stock}),
                content_type='application/json', **self.auth(user),
            )

    def set_ordering(self, user, accepting):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(
                RESTAURANTS_PUT,
                data=json.dumps({'id': str(self.restaurant_a.pk), 'accepting_orders': accepting}),
                content_type='application/json', **self.auth(user),
            )

    def submit_review(self, rating):
        from restaurants_app.controllers.diner_capability import issue_table_session
        order = Order.objects.create(
            restaurant=self.restaurant_a, table=self.table_a,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
            order_status=OrderStatus_Served,
        )
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                SUBMIT_REVIEW,
                data=json.dumps({'order': str(order.pk), 'overall_rating': rating,
                                 'comment': 'call me on 0772000000'}),
                content_type='application/json',
                HTTP_X_DINER_SESSION=issue_table_session(self.table_a),
            )
        return resp


class TenantIsolationTests(InboxTestBase):
    """A user of restaurant A can neither list nor mark B's notifications."""

    def setUp(self):
        super().setUp()
        self.b_row = RestaurantNotification.objects.create(
            restaurant=self.restaurant_b, module=MODULE_TABLES,
            kind=NotificationKind.ORDERING_PAUSED, dedupe_key='ordering',
            payload={'accepting_orders': False},
        )

    def test_listing_or_counting_a_foreign_restaurant_is_404(self):
        for url in (INBOX, COUNT):
            resp = self.client.get(url, {'restaurant': str(self.restaurant_b.pk)}, **self.auth(self.owner_a))
            self.assertEqual(resp.status_code, 404, url)
            self.assertEqual(resp.json(), {'status': 404, 'message': 'Not found'})

    def test_marking_a_foreign_notification_is_404_and_writes_nothing(self):
        resp = self.client.post(_read_url(self.b_row.pk), **self.auth(self.owner_a))
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(f'{READ_ALL}?restaurant={self.restaurant_b.pk}', **self.auth(self.owner_a))
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(NotificationRead.objects.exists())

    def test_unknown_malformed_and_missing_restaurant_answer_the_same_404(self):
        for params in ({}, {'restaurant': 'not-a-uuid'},
                       {'restaurant': '00000000-0000-0000-0000-000000000000'}):
            resp = self.client.get(INBOX, params, **self.auth(self.owner_a))
            self.assertEqual(resp.status_code, 404, params)
            self.assertEqual(resp.json(), {'status': 404, 'message': 'Not found'})
        self.assertEqual(self.client.post(_read_url('junk'), **self.auth(self.owner_a)).status_code, 404)

    def test_an_employee_nowhere_sees_nothing(self):
        self.assertEqual(self.inbox(self.outsider, self.restaurant_b).status_code, 404)

    def test_control_b_owner_sees_their_own(self):
        self.assertEqual(self.kinds(self.owner_b, self.restaurant_b), ['ordering_paused'])

    def test_unauthenticated_is_refused(self):
        resp = self.client.get(INBOX, {'restaurant': str(self.restaurant_b.pk)})
        self.assertIn(resp.status_code, (401, 403))

    def test_responses_are_no_store(self):
        resp = self.inbox(self.owner_b, self.restaurant_b)
        self.assertIn('no-store', resp['Cache-Control'])

    def test_inbox_routes_are_not_on_the_delegated_allowlist(self):
        from platform_admin_app.configs.delegation_scopes import ALLOWED_ROUTES
        self.assertFalse([r for (r, _m) in ALLOWED_ROUTES if 'notifications' in r])


class ModuleVisibilityTests(InboxTestBase):
    """Visibility follows can_user_access_module, at READ time."""

    def setUp(self):
        super().setUp()
        for module, kind, key in ((MODULE_REVIEWS, NotificationKind.REVIEW_LOW, 'review:1'),
                                  (MODULE_TABLES, NotificationKind.ITEM_SOLD_OUT, 'sold_out:x')):
            RestaurantNotification.objects.create(
                restaurant=self.restaurant_a, module=module, kind=kind, dedupe_key=key)

    def test_default_grids(self):
        self.assertEqual(self.kinds(self.owner_a, self.restaurant_a), ['item_sold_out', 'review_low'])
        # Front-of-house staff hold `tables` only by default.
        self.assertEqual(self.kinds(self.staff_a, self.restaurant_a), ['item_sold_out'])
        self.assertEqual(self.count(self.staff_a, self.restaurant_a), 1)

    def test_a_roles_and_access_change_applies_to_existing_rows_immediately(self):
        RestaurantRolePermission.objects.update_or_create(
            restaurant=self.restaurant_a, role=RESTAURANT_STAFF,
            defaults={'modules': {MODULE_TABLES: False, MODULE_REVIEWS: True}, 'deleted': False},
        )
        self.assertEqual(self.kinds(self.staff_a, self.restaurant_a), ['review_low'])
        RestaurantRolePermission.objects.filter(
            restaurant=self.restaurant_a, role=RESTAURANT_STAFF,
        ).update(modules={MODULE_TABLES: False, MODULE_REVIEWS: False})
        self.assertEqual(self.kinds(self.staff_a, self.restaurant_a), [])
        self.assertEqual(self.count(self.staff_a, self.restaurant_a), 0)
        self.assertEqual(RestaurantNotification.objects.count(), 2, 'no row was rewritten or deleted')

    def test_a_row_the_user_cannot_see_cannot_be_marked(self):
        review_row = RestaurantNotification.objects.get(kind=NotificationKind.REVIEW_LOW)
        resp = self.client.post(_read_url(review_row.pk), **self.auth(self.staff_a))
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(NotificationRead.objects.exists())


class ReadStateTests(InboxTestBase):
    """Unread = no read record at or after occurred_at (amendment 4)."""

    def setUp(self):
        super().setUp()
        self.row = RestaurantNotification.objects.create(
            restaurant=self.restaurant_a, module=MODULE_TABLES,
            kind=NotificationKind.ORDERING_PAUSED, dedupe_key='ordering')

    def test_mark_one_then_count(self):
        self.assertEqual(self.count(self.manager_a, self.restaurant_a), 1)
        resp = self.client.post(_read_url(self.row.pk), **self.auth(self.manager_a))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.count(self.manager_a, self.restaurant_a), 0)
        self.assertEqual(self.count(self.owner_a, self.restaurant_a), 1, 'read state is per user')
        listed = self.inbox(self.manager_a, self.restaurant_a).json()['data']['results'][0]
        self.assertTrue(listed['read'])

    def test_mark_all(self):
        RestaurantNotification.objects.create(
            restaurant=self.restaurant_a, module=MODULE_TABLES,
            kind=NotificationKind.ITEM_SOLD_OUT, dedupe_key='sold_out:y')
        resp = self.client.post(f'{READ_ALL}?restaurant={self.restaurant_a.pk}', **self.auth(self.manager_a))
        self.assertEqual(resp.json()['data']['marked'], 2)
        self.assertEqual(self.count(self.manager_a, self.restaurant_a), 0)

    def test_a_realert_is_unread_again_and_keeps_the_read_record(self):
        self.client.post(_read_url(self.row.pk), **self.auth(self.manager_a))
        RestaurantNotification.objects.filter(pk=self.row.pk).update(
            occurred_at=timezone.now() + timedelta(seconds=1))
        self.assertEqual(self.count(self.manager_a, self.restaurant_a), 1)
        self.assertEqual(NotificationRead.objects.filter(user=self.manager_a).count(), 1)

    def test_the_read_window_hides_old_rows(self):
        RestaurantNotification.objects.filter(pk=self.row.pk).update(
            occurred_at=timezone.now() - producers.READ_WINDOW - timedelta(minutes=1))
        self.assertEqual(self.kinds(self.manager_a, self.restaurant_a), [])

    def test_unread_count_is_flat_in_the_number_of_rows(self):
        def measure():
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            with CaptureQueriesContext(connection) as ctx:
                self.count(self.manager_a, self.restaurant_a)
            return len(ctx.captured_queries)
        few = measure()
        for i in range(20):
            RestaurantNotification.objects.create(
                restaurant=self.restaurant_a, module=MODULE_TABLES,
                kind=NotificationKind.ITEM_SOLD_OUT, dedupe_key=f'sold_out:{i}')
        self.assertEqual(measure(), few)


class SoldOutProducerTests(InboxTestBase):

    def test_fires_after_commit_never_before(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            resp = self.client.put(
                _stock_url(self.item.pk), data=json.dumps({'in_stock': False}),
                content_type='application/json', **self.auth(self.manager_a))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(RestaurantNotification.objects.exists(), 'nothing before commit')
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        row = RestaurantNotification.objects.get()
        self.assertEqual((row.kind, row.module, row.dedupe_key),
                         ('item_sold_out', MODULE_TABLES, f'sold_out:{self.item.pk}'))
        self.assertEqual(row.payload, {'menu_item_id': str(self.item.pk), 'item_name': 'Rolex'})

    def test_the_actor_is_not_notified_others_are(self):
        self.sell_out(self.manager_a)
        self.assertEqual(self.kinds(self.manager_a, self.restaurant_a), [])
        self.assertEqual(self.kinds(self.owner_a, self.restaurant_a), ['item_sold_out'])
        self.assertEqual(self.kinds(self.staff_a, self.restaurant_a), ['item_sold_out'])
        self.assertEqual(
            self.inbox(self.owner_a, self.restaurant_a).json()['data']['results'][0]['actor_name'],
            'Moses')

    def test_back_in_stock_does_not_notify(self):
        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=False)
        self.assertEqual(self.sell_out(self.manager_a, in_stock=True).status_code, 200)
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_already_sold_out_does_not_notify(self):
        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=False)
        self.sell_out(self.manager_a, in_stock=False)
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_repeat_inside_ten_minutes_updates_the_payload_only(self):
        self.sell_out(self.manager_a)
        first = RestaurantNotification.objects.get()
        self.client.post(_read_url(first.pk), **self.auth(self.owner_a))
        self.assertEqual(self.sell_out(self.manager_a, in_stock=True).status_code, 200)
        MenuItem.objects.filter(pk=self.item.pk).update(name='Rolex (large)')
        self.assertEqual(self.sell_out(self.owner_a).status_code, 200)
        row = RestaurantNotification.objects.get()
        self.assertEqual(row.occurred_at, first.occurred_at, 'no re-alert')
        self.assertEqual(row.actor_id, self.manager_a.pk, 'payload only')
        self.assertEqual(row.payload['item_name'], 'Rolex (large)')
        self.assertEqual(self.count(self.owner_a, self.restaurant_a), 0, 'still read')

    def test_repeat_after_ten_minutes_realerts(self):
        self.sell_out(self.manager_a)
        row = RestaurantNotification.objects.get()
        self.client.post(_read_url(row.pk), **self.auth(self.staff_a))
        # Both the alert and the read happened more than ten minutes ago.
        earlier = timezone.now() - producers.SOLD_OUT_REALERT_AFTER - timedelta(minutes=1)
        RestaurantNotification.objects.filter(pk=row.pk).update(occurred_at=earlier)
        NotificationRead.objects.update(read_at=earlier)
        self.sell_out(self.manager_a, in_stock=True)
        self.assertEqual(self.sell_out(self.owner_a).status_code, 200)
        row.refresh_from_db()
        self.assertGreater(row.occurred_at, earlier)
        self.assertEqual(row.actor_id, self.owner_a.pk)
        self.assertEqual(RestaurantNotification.objects.count(), 1)
        self.assertEqual(self.count(self.staff_a, self.restaurant_a), 1, 'unread again')
        self.assertEqual(NotificationRead.objects.count(), 1, 'the read record survives')

    def test_a_forced_producer_failure_leaves_the_response_unchanged(self):
        with mock.patch('notifications_app.producers._upsert', side_effect=RuntimeError('boom')), \
                self.assertLogs('notifications_app.producers', level='ERROR'):
            resp = self.sell_out(self.manager_a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 200, 'message': 'Stock updated',
                                       'data': {'id': str(self.item.pk), 'in_stock': False}})
        self.assertFalse(MenuItem.objects.get(pk=self.item.pk).in_stock, 'the toggle committed')


class OrderingProducerTests(InboxTestBase):

    def test_pause_and_resume_each_realert(self):
        self.assertEqual(self.set_ordering(self.manager_a, False).status_code, 200)
        row = RestaurantNotification.objects.get()
        self.assertEqual((row.kind, row.module, row.payload),
                         ('ordering_paused', MODULE_TABLES, {'accepting_orders': False}))
        self.client.post(_read_url(row.pk), **self.auth(self.owner_a))
        first_at = row.occurred_at
        self.set_ordering(self.manager_a, True)
        row.refresh_from_db()
        self.assertEqual(row.kind, 'ordering_resumed')
        self.assertGreater(row.occurred_at, first_at)
        self.assertEqual(self.count(self.owner_a, self.restaurant_a), 1, 'every change re-alerts')
        self.assertEqual(RestaurantNotification.objects.count(), 1, 'one row per restaurant')

    def test_an_unchanged_save_writes_nothing(self):
        self.set_ordering(self.manager_a, True)
        self.assertFalse(RestaurantNotification.objects.exists())
        self.set_ordering(self.manager_a, False)
        before = RestaurantNotification.objects.get().occurred_at
        self.set_ordering(self.owner_a, False)
        self.assertEqual(RestaurantNotification.objects.get().occurred_at, before)

    def test_a_save_without_accepting_orders_does_not_notify(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.put(
                RESTAURANTS_PUT,
                data=json.dumps({'id': str(self.restaurant_a.pk), 'tagline': 'New'}),
                content_type='application/json', **self.auth(self.owner_a))
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_the_actor_is_not_notified(self):
        self.set_ordering(self.manager_a, False)
        self.assertEqual(self.kinds(self.manager_a, self.restaurant_a), [])
        self.assertEqual(self.kinds(self.staff_a, self.restaurant_a), ['ordering_paused'])

    def test_a_forced_producer_failure_leaves_the_response_unchanged(self):
        baseline = self.set_ordering(self.manager_a, False)
        RestaurantNotification.objects.all().delete()
        with mock.patch('notifications_app.producers._upsert', side_effect=RuntimeError('boom')), \
                self.assertLogs('notifications_app.producers', level='ERROR'):
            resp = self.set_ordering(self.manager_a, True)
        self.assertEqual(resp.status_code, baseline.status_code)
        self.assertEqual(resp.json()['status'], baseline.json()['status'])
        self.assertTrue(Restaurant.objects.get(pk=self.restaurant_a.pk).accepting_orders)

    def test_fires_after_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.client.put(
                RESTAURANTS_PUT,
                data=json.dumps({'id': str(self.restaurant_a.pk), 'accepting_orders': False}),
                content_type='application/json', **self.auth(self.manager_a))
        self.assertFalse(RestaurantNotification.objects.exists())
        self.assertEqual(len(callbacks), 1)


class LowReviewProducerTests(InboxTestBase):

    def test_a_low_review_notifies_reviews_holders_only(self):
        self.assertEqual(self.submit_review(2).status_code, 201)
        row = RestaurantNotification.objects.get()
        review = Review.objects.get()
        self.assertEqual((row.kind, row.module, row.actor_id),
                         ('review_low', MODULE_REVIEWS, None))
        self.assertEqual(row.payload, {'review_id': review.pk, 'rating': 2})
        self.assertNotIn('comment', json.dumps(row.payload), 'no diner free text')
        self.assertNotIn('0772000000', json.dumps(row.payload))
        self.assertEqual(self.kinds(self.owner_a, self.restaurant_a), ['review_low'])
        self.assertEqual(self.kinds(self.staff_a, self.restaurant_a), [])

    def test_a_review_at_the_threshold_does_not_notify(self):
        self.assertEqual(self.submit_review(4).status_code, 201)
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_a_forced_producer_failure_leaves_the_response_unchanged(self):
        with mock.patch('notifications_app.producers._upsert', side_effect=RuntimeError('boom')), \
                self.assertLogs('notifications_app.producers', level='ERROR'):
            resp = self.submit_review(1)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['message'], 'Thank you! Your review has been submitted.')
        self.assertTrue(Review.objects.exists())

    def test_one_alert_per_review(self):
        self.submit_review(1)
        review = Review.objects.get()
        before = RestaurantNotification.objects.get().occurred_at
        producers.notify_low_review(review)
        self.assertEqual(RestaurantNotification.objects.get().occurred_at, before)


class ProducerTransactionTests(InboxTestBase):

    def test_a_rolled_back_change_is_never_announced(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    producers.schedule(
                        producers.notify_ordering_changed, self.restaurant_a.pk, False, None)
                    raise RuntimeError('the triggering change failed')
            except RuntimeError:
                pass
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_prune_is_per_restaurant_and_bounded(self):
        old = timezone.now() - producers.RETAIN_FOR - timedelta(days=1)
        for i in range(3):
            RestaurantNotification.objects.create(
                restaurant=self.restaurant_a, module=MODULE_TABLES,
                kind=NotificationKind.ITEM_SOLD_OUT, dedupe_key=f'sold_out:old{i}', occurred_at=old)
        RestaurantNotification.objects.create(
            restaurant=self.restaurant_b, module=MODULE_TABLES,
            kind=NotificationKind.ITEM_SOLD_OUT, dedupe_key='sold_out:oldb', occurred_at=old)
        Restaurant.objects.filter(pk=self.restaurant_a.pk).update(accepting_orders=False)
        with mock.patch.object(producers, 'PRUNE_BATCH', 2):
            producers.notify_ordering_changed(self.restaurant_a.pk, False, None)
        self.assertEqual(
            RestaurantNotification.objects.filter(restaurant=self.restaurant_a, occurred_at=old).count(), 1)
        self.assertTrue(RestaurantNotification.objects.filter(dedupe_key='sold_out:oldb').exists())

class OutOfOrderCallbackTests(InboxTestBase):
    """Codex P2 on #379: two overlapping changes can run their after-commit
    callbacks in the opposite order to their commits. A stale event must not
    overwrite the state that actually stands."""

    def _put_ordering(self, user, accepting):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            resp = self.client.put(
                RESTAURANTS_PUT,
                data=json.dumps({'id': str(self.restaurant_a.pk), 'accepting_orders': accepting}),
                content_type='application/json', **self.auth(user))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(callbacks), 1)
        return callbacks[0]

    def _put_stock(self, user, in_stock):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            resp = self.client.put(
                _stock_url(self.item.pk), data=json.dumps({'in_stock': in_stock}),
                content_type='application/json', **self.auth(user))
        self.assertEqual(resp.status_code, 200, resp.content)
        return callbacks

    def test_a_pause_callback_landing_after_a_resume_does_not_overwrite_it(self):
        pause = self._put_ordering(self.manager_a, False)
        resume = self._put_ordering(self.owner_a, True)
        resume()   # the later change's callback runs first...
        pause()    # ...and the earlier one lands after it
        row = RestaurantNotification.objects.get()
        self.assertEqual((row.kind, row.payload, row.actor_id),
                         ('ordering_resumed', {'accepting_orders': True}, self.owner_a.pk))

    def test_a_stale_pause_with_no_row_yet_writes_nothing(self):
        pause = self._put_ordering(self.manager_a, False)
        self._put_ordering(self.owner_a, True)
        pause()
        self.assertFalse(RestaurantNotification.objects.exists())

    def test_control_in_order_callbacks_still_announce_the_latest(self):
        pause = self._put_ordering(self.manager_a, False)
        pause()
        self.assertEqual(RestaurantNotification.objects.get().kind, 'ordering_paused')
        resume = self._put_ordering(self.owner_a, True)
        resume()
        self.assertEqual(RestaurantNotification.objects.get().kind, 'ordering_resumed')

    def test_a_sold_out_callback_landing_after_back_in_stock_writes_nothing(self):
        sold_out = self._put_stock(self.manager_a, False)
        self.assertEqual(self._put_stock(self.owner_a, True), [], 'back in stock schedules nothing')
        sold_out[0]()
        self.assertFalse(RestaurantNotification.objects.exists())
