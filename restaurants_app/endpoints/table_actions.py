from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from misc_app.controllers.decode_auth_token import decode_jwt_token
from users_app.controllers.permissions_check import can_user_access_module
from dinify_backend.configss.string_definitions import MODULE_TABLES
from restaurants_app.models import Table, Reservation
from restaurants_app.serializers import SerializerPublicGetTable
from restaurants_app.controllers.diner_capability import issue_qr_credential
from restaurants_app.controllers.qr_disclosure import qr_disclosure_policy
from orders_app.controllers.occupancy import ongoing_order_q
from orders_app.models import Order


TABLE_STATUS_CHOICES = {
    'available', 'seated', 'bill_requested', 'dirty', 'out_of_service'
}


# The stable machine reason clear and transfer refuse with. A client branches on
# it; the message beside it is for a person and may be reworded.
REASON_TABLE_HAS_OPEN_VISIT = 'table_has_open_visit'

# The geometry and layout columns the floor-plan save may write, and nothing else.
FLOOR_PLAN_FIELDS = ('floor_x', 'floor_y', 'floor_width', 'floor_height')


def _lock_tables(table_ids, restaurant_id=None):
    """Re-read the named, non-deleted tables `FOR UPDATE` in ONE statement, in
    PRIMARY-KEY order, and return them keyed by primary key.

    THE ORDER IS THE POINT. Two requests that lock the same pair of tables in
    opposite orders deadlock; two that both take them in primary-key order
    cannot, because whichever reaches the lower key first holds it while the
    other waits there holding nothing. PostgreSQL takes the row locks of an
    `ORDER BY ... FOR UPDATE` as the sorted rows are returned, so the
    `order_by('pk')` is what fixes the order. It must be explicit: `Table`'s
    `Meta.ordering` is `number`, not the key.

    No `select_related`, so the lock covers the `tables` rows and nothing else.
    Must be called inside the transaction that writes, or the lock is released
    by the statement that took it.
    """
    queryset = Table.objects.select_for_update().filter(
        pk__in=list(table_ids), deleted=False)
    if restaurant_id is not None:
        queryset = queryset.filter(restaurant_id=restaurant_id)
    return {table.pk: table for table in queryset.order_by('pk')}


def _has_ongoing_order(table_id):
    """Does this table have an order that occupies it, by THE one occupancy rule?

    Asked under the `Table` row lock. Every path that can make an order occupy a
    table (submit, kitchen recall) takes that same row lock first — the order
    path's `Table -> Order` — so the answer cannot change until this
    transaction ends. It reads `orders` with a plain SELECT and locks no order
    row: the `Table` lock is the serialization point, and taking an `Order` lock
    here would only add a participant to the order path's lock order.
    """
    return Order.objects.filter(ongoing_order_q(), table_id=table_id).exists()


def _open_visit_refusal(message):
    return Response(
        {'status': 409, 'message': message, 'reason': REASON_TABLE_HAS_OPEN_VISIT},
        status=409,
    )


def _table_vanished():
    return Response({'status': 404, 'message': 'Table not found'}, status=404)


def _qr_context(request):
    """
    The response-entitlement context every table response built here carries.

    These are ORDINARY, ALREADY-AUTHORIZED action responses — every one of them
    is behind this endpoint's own `can_user_access_module(..., MODULE_TABLES)`
    gate, and a delegated session is refused the whole route before dispatch
    (`table-actions` is absent from `ALLOWED_ROUTES`). They are given the context
    EXPLICITLY all the same: `SerializerPublicGetTable` now WITHHOLDS BY DEFAULT,
    so a response constructed without one would silently stop carrying a field
    these contracts already carry. Fail-closed defaults must not regress a
    surface that was never the exposure.

    Resolved once per response and shared by every serializer in it, so a
    multi-table answer (transfer) costs the scope query once rather than per row.
    """
    return {'qr_policy': qr_disclosure_policy(request)}


class TableActionsEndpoint(APIView):
    """Endpoint for table lifecycle actions (seat, clear, transfer, etc.)."""

    def post(self, request, action):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        dispatch = {
            'seat': self._seat,
            'clear': self._clear,
            'transfer': self._transfer,
            'update-status': self._update_status,
            'update-floor-plan': self._update_floor_plan,
            'regenerate-qr': self._regenerate_qr,
        }

        handler = dispatch.get(action)
        if not handler:
            return Response(
                {'status': 400, 'message': f'Unknown action: {action}'},
                status=400
            )

        return handler(request)

    def _get_table_or_error(self, table_id):
        """Look up a non-deleted table by ID. Returns (table, None) or (None, Response)."""
        try:
            table = Table.objects.get(id=table_id, deleted=False)
            return table, None
        except Table.DoesNotExist:
            return None, Response(
                {'status': 404, 'message': 'Table not found'}, status=404
            )

    # ------------------------------------------------------------------
    # action = "seat"
    # ------------------------------------------------------------------
    def _seat(self, request):
        data = request.data
        table_id = data.get('table_id')
        if not table_id:
            return Response(
                {'status': 400, 'message': 'table_id is required'}, status=400
            )

        table, err = self._get_table_or_error(table_id)
        if err:
            return err

        if not can_user_access_module(
            request.user, str(table.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        # LOCKED RE-READ, NARROW WRITE (Step 2, BE-A) — the `_update_status`
        # fix, applied here. The instance above was read unlocked, and a bare
        # `table.save()` writes EVERY column from it, so a QR regeneration
        # committed in between had its `qr_version` written back: every diner
        # credential it had just revoked came back to life. Seat decides
        # `status` and nothing else, so that is all it writes. Seat does NOT
        # consult occupancy; it is unchanged in behaviour apart from the write.
        #
        # LOCK ORDER: `Table`, then (unlocked) the reservation. No `Restaurant`
        # row and no admission advisory lock, so this can block an order path
        # but cannot cycle against it.
        with transaction.atomic():
            table = _lock_tables([table.pk]).get(table.pk)
            if table is None:
                return _table_vanished()

            table.status = 'seated'
            table.save(update_fields=['status'])

            # If a reservation is provided, mark it as seated
            reservation_id = data.get('reservation_id')
            if reservation_id:
                try:
                    # Scope the reservation to the gated table's restaurant so a
                    # foreign reservation_id can't be mutated into another
                    # tenant's row. A foreign id now falls into the same
                    # non-fatal silent-ignore path an unknown id already did: the
                    # table still seats and the attacker learns nothing.
                    reservation = Reservation.objects.get(
                        id=reservation_id,
                        restaurant_id=table.restaurant_id,
                        deleted=False,
                    )
                    reservation.status = 'seated'
                    reservation.seated_at = timezone.now()
                    reservation.table = table
                    reservation.save(
                        update_fields=['status', 'seated_at', 'table'])
                except Reservation.DoesNotExist:
                    pass  # Non-fatal: table is already seated

        return Response({
            'status': 200,
            'message': 'Table seated successfully',
            'data': SerializerPublicGetTable(table, context=_qr_context(request)).data
        }, status=200)

    # ------------------------------------------------------------------
    # action = "clear"
    # ------------------------------------------------------------------
    def _clear(self, request):
        data = request.data
        table_id = data.get('table_id')
        if not table_id:
            return Response(
                {'status': 400, 'message': 'table_id is required'}, status=400
            )

        table, err = self._get_table_or_error(table_id)
        if err:
            return err

        if not can_user_access_module(
            request.user, str(table.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        mark_as = data.get('mark_as', 'dirty')
        if mark_as not in ('dirty', 'available'):
            return Response(
                {'status': 400, 'message': 'mark_as must be "dirty" or "available"'},
                status=400
            )

        # LOCK, REFUSE AN OPEN VISIT, NARROW WRITE (Step 2, BE-A).
        #
        # Clearing a table that still has an order in flight declares the
        # table free while a party is still eating at it — and the next diner
        # to scan it is then seated on top of them. So the occupancy rule is
        # asked UNDER the row lock, where no submit or kitchen recall can change
        # the answer before this commits, and an ongoing order (held rounds
        # included, once Step 2 creates them) is a 409 with a stable reason.
        # A served or cancelled order does not occupy the table and does not
        # block it.
        #
        # Then only `status` is written, for the reason seat gives above.
        with transaction.atomic():
            table = _lock_tables([table.pk]).get(table.pk)
            if table is None:
                return _table_vanished()

            if _has_ongoing_order(table.pk):
                return _open_visit_refusal(
                    'This table still has an open order. Serve or cancel it '
                    'before clearing the table.'
                )

            table.status = mark_as
            table.save(update_fields=['status'])

        return Response({
            'status': 200,
            'message': 'Table cleared successfully',
            'data': SerializerPublicGetTable(table, context=_qr_context(request)).data
        }, status=200)

    # ------------------------------------------------------------------
    # action = "transfer"
    # ------------------------------------------------------------------
    def _transfer(self, request):
        data = request.data
        source_id = data.get('source_table_id')
        dest_id = data.get('destination_table_id')
        if not source_id or not dest_id:
            return Response(
                {'status': 400, 'message': 'source_table_id and destination_table_id are required'},
                status=400
            )

        source, err = self._get_table_or_error(source_id)
        if err:
            return err
        dest, err = self._get_table_or_error(dest_id)
        if err:
            return err

        if source.restaurant_id != dest.restaurant_id:
            return Response(
                {'status': 400, 'message': 'Both tables must belong to the same restaurant'},
                status=400
            )

        if not can_user_access_module(
            request.user, str(source.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        # LOCK BOTH, REFUSE AN OPEN VISIT AT EITHER, NARROW WRITE (Step 2,
        # BE-A).
        #
        # A transfer moves the PARTY's status and its seated reservation. It
        # moves NO ORDERS: an order stays on the table it was placed at. So:
        #
        #   * a SOURCE with an ongoing order would be marked dirty while its
        #     order still belongs to it, leaving the order at a table the floor
        #     now says is empty;
        #   * a DESTINATION with an ongoing order already has a party, and
        #     seating a second one there merges two bills nobody merged.
        #
        # Both are refused 409 with the same stable reason. Both rows are locked
        # in ONE statement in primary-key order, so two transfers between the
        # same pair in opposite directions serialize instead of deadlocking.
        with transaction.atomic():
            locked = _lock_tables([source.pk, dest.pk])
            source = locked.get(source.pk)
            dest = locked.get(dest.pk)
            if source is None or dest is None:
                return _table_vanished()

            if _has_ongoing_order(source.pk):
                return _open_visit_refusal(
                    'The source table still has an open order, and a transfer '
                    'does not move orders. Serve or cancel it first.'
                )
            if _has_ongoing_order(dest.pk):
                return _open_visit_refusal(
                    'The destination table already has an open order. Choose '
                    'a free table.'
                )

            source.status = 'dirty'
            source.save(update_fields=['status'])

            dest.status = 'seated'
            dest.save(update_fields=['status'])

            # Move any active reservation from source to destination
            Reservation.objects.filter(
                table=source, status='seated', deleted=False
            ).update(table=dest)

        qr_context = _qr_context(request)
        return Response({
            'status': 200,
            'message': 'Party transferred successfully',
            'data': {
                'source': SerializerPublicGetTable(source, context=qr_context).data,
                'destination': SerializerPublicGetTable(dest, context=qr_context).data,
            }
        }, status=200)

    # ------------------------------------------------------------------
    # action = "update-status"
    # ------------------------------------------------------------------
    def _update_status(self, request):
        data = request.data
        table_id = data.get('table_id')
        new_status = data.get('status')
        if not table_id or not new_status:
            return Response(
                {'status': 400, 'message': 'table_id and status are required'},
                status=400
            )

        if new_status not in TABLE_STATUS_CHOICES:
            return Response(
                {'status': 400, 'message': f'Invalid status. Must be one of: {", ".join(sorted(TABLE_STATUS_CHOICES))}'},
                status=400
            )

        table, err = self._get_table_or_error(table_id)
        if err:
            return err

        if not can_user_access_module(
            request.user, str(table.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        # THE ROW IS RE-READ UNDER A LOCK AND ONLY TWO COLUMNS ARE WRITTEN
        # (D06). It used to decide from the unlocked instance above and then
        # `table.save()` — a FULL-ROW write of every column on an instance
        # loaded before the decision — so any concurrent write to the same table
        # was silently reverted by whichever request saved last. Two of those
        # reverts matter enough to name:
        #
        #   * `qr_version`. Regenerating a table's QR bumps it, and that bump is
        #     what REVOKES every diner credential and session outstanding for the
        #     table. A status change loaded before the regeneration wrote the old
        #     generation back, un-revoking them all — a security control undone
        #     by an unrelated operator action, with nothing reported.
        #   * `qr_mode` / `enabled` / `deleted`. The facts the order path now
        #     consults at creation AND at acceptance; reverting one re-opens
        #     ordering at a table somebody had just closed.
        #
        # The lock also makes this writer participate in the order path's
        # serialization rather than running beside it: both order boundaries take
        # this exact row `FOR UPDATE`, so a status change and an order either
        # happen in a definite order or one waits.
        #
        # LOCK ORDER: the `Table` row and nothing else. It takes no `Restaurant`
        # row and never reaches for the admission advisory lock, so it can block
        # an order path but cannot cycle against it or against the lifecycle
        # transition.
        with transaction.atomic():
            try:
                table = Table.objects.select_for_update().get(pk=table.pk)
            except Table.DoesNotExist:
                return Response(
                    {'status': 404, 'message': 'Table not found'}, status=404)

            old_status = table.status
            table.status = new_status

            if new_status == 'out_of_service':
                table.is_active = False
            elif old_status == 'out_of_service':
                table.is_active = True

            # `update_fields` is the fix, not the lock: a lock stops a
            # CONCURRENT revert, and writing only what this action decides stops
            # this action from reverting anything at all.
            table.save(update_fields=['status', 'is_active'])

        return Response({
            'status': 200,
            'message': 'Table status updated successfully',
            'data': SerializerPublicGetTable(table, context=_qr_context(request)).data
        }, status=200)

    # ------------------------------------------------------------------
    # action = "update-floor-plan"
    # ------------------------------------------------------------------
    def _update_floor_plan(self, request):
        data = request.data
        restaurant_id = data.get('restaurant')
        tables_data = data.get('tables')

        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant is required'}, status=400
            )
        if not tables_data or not isinstance(tables_data, list):
            return Response(
                {'status': 400, 'message': 'tables array is required'}, status=400
            )

        if not can_user_access_module(request.user, restaurant_id, MODULE_TABLES):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        # ONE LOCKING STATEMENT, PRIMARY-KEY ORDER, GEOMETRY ONLY (Step 2,
        # BE-A). This used to `get()` each table unlocked and `save()` the whole
        # row, so a floor-plan save reverted any column written in between —
        # `qr_version` (un-revoking a QR regeneration), `status`, `qr_mode`,
        # `enabled`. It now locks every row it will write in one statement, in
        # primary-key order (see `_lock_tables`: the client's list order must
        # not decide the lock order, or two saves of one floor deadlock), and
        # writes only the geometry columns each entry actually sent.
        #
        # Behaviour is otherwise unchanged: an entry with no id, or naming a
        # table that is unknown, deleted or at another restaurant, is skipped;
        # `updated_count` counts the entries that named a table here.
        updated = 0
        with transaction.atomic():
            wanted = [
                entry.get('id') for entry in tables_data if entry.get('id')
            ]
            locked = _lock_tables(wanted, restaurant_id) if wanted else {}
            # Ids are matched as the old per-row `get(id=...)` matched them:
            # through the key field's own conversion, so every spelling the
            # lookup accepted still names the same row.
            to_pk = Table._meta.pk.to_python

            for entry in tables_data:
                table_id = entry.get('id')
                if not table_id:
                    continue
                table = locked.get(to_pk(table_id))
                if table is None:
                    continue

                fields = []
                for field in FLOOR_PLAN_FIELDS:
                    if field in entry:
                        setattr(table, field, float(entry[field]))
                        fields.append(field)

                if fields:
                    table.save(update_fields=fields)
                updated += 1

        return Response({
            'status': 200,
            'message': f'{updated} table(s) updated successfully',
            'data': {'updated_count': updated}
        }, status=200)

    # ------------------------------------------------------------------
    # action = "regenerate-qr"
    # ------------------------------------------------------------------
    def _regenerate_qr(self, request):
        # Rotate the table's QR generation. Bumping qr_version invalidates every
        # outstanding QR credential AND live diner session for this table (they
        # carry the old generation and fail the verifier's generation re-check) —
        # a real revocation, with zero stored secrets. Owner/manager-gated via the
        # tables module, same as every other action here.
        data = request.data
        table_id = data.get('table_id')
        if not table_id:
            return Response(
                {'status': 400, 'message': 'table_id is required'}, status=400
            )

        table, err = self._get_table_or_error(table_id)
        if err:
            return err

        if not can_user_access_module(
            request.user, str(table.restaurant_id), MODULE_TABLES,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        # F()-expression bump is atomic and race-safe under concurrent regen taps.
        with transaction.atomic():
            Table.objects.filter(id=table.id).update(
                qr_version=F('qr_version') + 1,
                qr_regenerated_at=timezone.now(),
                has_qr=True,
            )
        table.refresh_from_db(
            fields=['qr_version', 'qr_regenerated_at', 'has_qr']
        )

        payload = {
            'id': str(table.id),
            'number': table.number,
            'qr_version': table.qr_version,
            'qr_regenerated_at': table.qr_regenerated_at,
        }
        # The fresh opaque credential to encode into the reprinted QR sticker —
        # bound to restaurant+table+new generation.
        #
        # Gated on the SAME response-entitlement decision as every other table
        # builder, rather than on this route being off the delegated allowlist.
        # A route allowlist is a statement about which requests arrive; it is not
        # a statement about the principal, and `can_user_access_module` above
        # deliberately answers True for a delegated caller too. Asking the one
        # question everywhere is what keeps that from being an exception nobody
        # revisits when the allowlist next changes.
        #
        # It cannot strand a legitimate operator: this endpoint has already
        # required ordinary `tables` access at this exact restaurant, and
        # `can_user_access_module` and `get_module_restaurant_ids` resolve that
        # from the same employment, lifecycle and override rows — so for a
        # non-delegated caller who reached here the scope necessarily contains it.
        if qr_disclosure_policy(request).allows(table.restaurant_id):
            payload['qr_credential'] = issue_qr_credential(
                table.restaurant_id, table.id, table.qr_version,
            )

        return Response({
            'status': 200,
            'message': 'QR code regenerated successfully. Previously issued QR '
                       'codes and diner sessions for this table are now invalid.',
            'data': payload,
        }, status=200)
