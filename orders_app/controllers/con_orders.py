import logging

from decimal import Decimal
from django.utils import timezone
from django.db.models import Sum
from typing import Optional, Union
from users_app.models import User
from dinify_backend.configss.messages import MESSAGES
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from restaurants_app.models import Restaurant, MenuItem, Table
from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    TransactionStatus_Success
)
from orders_app.models import Order, OrderItem
from orders_app.serializers import SerializerPutOrderItem
from finance_app.models import DinifyTransaction
from orders_app.controllers.orders.serializers import serialize_order_details
from orders_app.controllers.services.create_order import _create_order
from restaurants_app.controllers.menu_publication import (
    NOT_ON_MENU_MESSAGE, validate_order_selections,
)
from orders_app.controllers.services.order_admission import (
    STAGE_CREATE,
    evaluate,
)

logger = logging.getLogger(__name__)

# NOT_ON_MENU_MESSAGE is the canonical opaque menu-rejection string, now owned by
# the menu-publication policy (restaurants_app/controllers/menu_publication.py) and
# re-exported here so existing importers — and tests — keep resolving it from
# con_orders unchanged.


class ConOrder:
    @staticmethod
    def normalize_selected_modifiers(menu_item: MenuItem, selected_modifiers) -> dict:
        """
        Validate + canonicalize a diner's grouped modifier selection against the
        ordered MenuItem's OWN server-side ``options`` definition.

        Returns ``{'status': 200, 'selected_modifiers': <canonical dict>}`` or a
        controlled ``{'status': 400, 'message': ...}`` envelope. It NEVER raises —
        malformed client input OR malformed operator ``options`` fail closed with a
        400 rather than a 500.

        Canonical representation: ``{group_id: [choice_id, ...]}`` where the groups
        and the choices within each group follow the item's OWN ``options`` definition
        order, duplicate choices are collapsed, and empty optional groups are omitted.
        This single representation is the sole input to min/max counting, existing-line
        comparison, server-side pricing, snapshot construction and persistence — so a
        selection compares/prices/persists identically regardless of the order or
        duplication the client happened to send. Costs are NEVER read from client
        input (only ids are handled here; pricing stays in
        ``determine_effective_unit_price``). Because the parsed selection is already a
        dict, duplicate group identifiers cannot exist (JSON parsing collapses them).
        """
        if selected_modifiers is None:
            selected_modifiers = {}
        if not isinstance(selected_modifiers, dict):
            return {
                'status': 400,
                'message': f'Invalid modifier selections for item, {menu_item.name}'
            }

        modifier_data = menu_item.options if isinstance(menu_item.options, dict) else {}
        has_modifiers = bool(modifier_data.get('hasModifiers'))
        raw_groups = modifier_data.get('groups') if has_modifiers else []
        if not isinstance(raw_groups, list):
            raw_groups = []
        groups = [g for g in raw_groups if isinstance(g, dict) and g.get('id') is not None]
        groups_by_id = {}
        for group in groups:
            groups_by_id.setdefault(group.get('id'), group)

        # Item with no active modifiers: an absent/null/empty selection normalizes to
        # {}, but a non-empty modifier object is rejected rather than silently
        # persisted as unrelated client data.
        if not has_modifiers or not groups_by_id:
            if not selected_modifiers:
                return {'status': 200, 'selected_modifiers': {}}
            return {
                'status': 400,
                'message': f'Item {menu_item.name} does not accept modifier selections.'
            }

        # Reject any submitted group that is not defined on THIS item's options.
        for submitted_group_id in selected_modifiers.keys():
            if submitted_group_id not in groups_by_id:
                return {
                    'status': 400,
                    'message': f'Invalid modifier group for item, {menu_item.name}'
                }

        canonical = {}
        # Iterate the item's OWN group order so (a) a minimum is enforced even for an
        # omitted required group and (b) the canonical dict is deterministically
        # ordered by menu definition.
        for group in groups:
            group_id = group.get('id')
            submitted = selected_modifiers.get(group_id, [])
            if group_id in selected_modifiers and not isinstance(submitted, list):
                return {
                    'status': 400,
                    'message': f'Invalid modifier selection for item, {menu_item.name}'
                }
            choice_ids = submitted if isinstance(submitted, list) else []

            raw_choices = group.get('choices')
            if not isinstance(raw_choices, list):
                raw_choices = []
            defined_choice_ids = [
                choice.get('id') for choice in raw_choices
                if isinstance(choice, dict) and choice.get('id') is not None
            ]
            defined_choice_set = set(defined_choice_ids)

            # De-dupe submitted choices (dict.fromkeys preserves first-seen order only
            # to detect unknowns; the persisted order below is menu-definition order).
            unique_submitted = list(dict.fromkeys(choice_ids))
            for choice_id in unique_submitted:
                if choice_id not in defined_choice_set:
                    return {
                        'status': 400,
                        'message': f'Invalid modifier choice for item, {menu_item.name}'
                    }
            submitted_set = set(unique_submitted)
            canonical_choice_ids = [
                choice_id for choice_id in defined_choice_ids
                if choice_id in submitted_set
            ]

            # min/max enforced on the UNIQUE selected count (0 for an omitted group).
            min_selections = group.get('minSelections') or 0
            max_selections = group.get('maxSelections') or 0
            selected_count = len(canonical_choice_ids)
            if selected_count < min_selections:
                return {
                    'status': 400,
                    'message': f"Item {menu_item.name} requires at least {min_selections} option selections."
                }
            if max_selections and selected_count > max_selections:
                return {
                    'status': 400,
                    'message': f"Item {menu_item.name} allows a maximum of {max_selections} option selections."
                }

            # Omit empty optional groups from the canonical form.
            if canonical_choice_ids:
                canonical[group_id] = canonical_choice_ids

        return {'status': 200, 'selected_modifiers': canonical}

    @staticmethod
    def normalize_order_items(restaurant, order_items: list) -> dict:
        """
        Batch counterpart of ``normalize_selected_modifiers`` for the authoritative
        order-creation transaction. For each line it resolves the MenuItem
        tenant-scoped, canonicalizes its ``selected_modifiers``, and returns a NEW list
        of shallow-copied items whose ``selected_modifiers`` is the canonical form —
        the caller's original items are never mutated in place.

        Returns ``{'status': 200, 'items': [<normalized copies>]}`` or the first
        controlled ``{'status': 400, 'message': ...}`` rejection.
        """
        normalized = []
        for item in order_items:
            if not isinstance(item, dict) or item.get('item') is None:
                return {
                    'status': 400,
                    'message': 'Each order item must include an item and a quantity.'
                }
            try:
                menu_item = MenuItem.objects.get(
                    pk=item['item'], section__restaurant=restaurant
                )
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}

            result = ConOrder.normalize_selected_modifiers(
                menu_item, item.get('selected_modifiers')
            )
            if result.get('status') != 200:
                return result

            new_item = dict(item)
            new_item['selected_modifiers'] = result['selected_modifiers']
            normalized.append(new_item)
        return {'status': 200, 'items': normalized}

    @staticmethod
    def check_options_requirements(order_items: list) -> dict:
        """
        Non-authoritative endpoint preflight gate: validate every line's modifier
        selection against its item's options. Delegates to the single canonical
        normalizer (``normalize_selected_modifiers``) so group/choice validity, de-dup
        and min/max have exactly ONE implementation. The authoritative in-transaction
        transform is ``normalize_order_items`` (which additionally captures the
        canonical value); this wrapper only surfaces an early rejection before the
        atomic block and persists nothing.
        """
        for item in order_items:
            try:
                menu_item = MenuItem.objects.get(pk=item['item'])
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
            result = ConOrder.normalize_selected_modifiers(
                menu_item, item.get('selected_modifiers')
            )
            if result.get('status') != 200:
                return result
        return {'status': 200}

    @staticmethod
    def check_extras_requirements(order_items: list) -> dict:
        for item in order_items:
            menu_item = MenuItem.objects.get(pk=item['item'])
            if not menu_item.has_extras:
                continue
            selected_count = len(item.get('extras') or [])
            min_extras = menu_item.extras_min_selections or 0
            max_extras = menu_item.extras_max_selections  # None/0 => unlimited
            if selected_count < min_extras:
                return {
                    'status': 400,
                    'message': f"Item {menu_item.name} requires at least {min_extras} extra selection(s)."
                }
            if max_extras and selected_count > max_extras:
                return {
                    'status': 400,
                    'message': f"Item {menu_item.name} allows a maximum of {max_extras} extra selection(s)."
                }
        return {'status': 200}

    @staticmethod
    def construct_option_items(item: dict) -> list:
        menu_item = MenuItem.objects.get(pk=item['item'])
        selected_modifiers = item.get('selected_modifiers') or {}
        modifier_data = menu_item.options or {}
        if not modifier_data.get('hasModifiers') or not selected_modifiers:
            return []

        groups_by_id = {g.get('id'): g for g in modifier_data.get('groups', [])}
        selected_options = []
        for group_id, choice_ids in selected_modifiers.items():
            group = groups_by_id.get(group_id)
            if group is None or not choice_ids:
                continue
            choices_by_id = {c.get('id'): c for c in group.get('choices', [])}
            # De-dupe so the displayed option cost matches the charged cost
            # (determine_effective_unit_price also charges each choice once).
            resolved_choices = [
                choices_by_id[cid] for cid in dict.fromkeys(choice_ids) if cid in choices_by_id
            ]
            names_of_choices = ', '.join(c.get('name', '') for c in resolved_choices)
            cost_total = float(sum(
                Decimal(str(c.get('additionalCost', 0))) for c in resolved_choices
            ))
            selected_options.append({
                'name': group.get('name'),
                'cost': cost_total,
                'choices': names_of_choices,
            })
        return selected_options

    @staticmethod
    def any_present_ongoing_order(table: Table) -> dict:
        """
        Determine whether a table is occupied by an ongoing order.

        A table is occupied iff it has a SUBMITTED order — one that is not a
        draft (order_status != 'initiated'), not deleted, not cancelled, and
        whose fulfilment_status is not 'served'. An 'initiated' order is an
        unconfirmed draft that does NOT occupy the table (it claims the table
        only at submit). Occupancy otherwise keys off the kitchen-owned
        fulfilment axis (not payment_status), so a table frees up once the
        kitchen serves its order. Returns the most recent such order.
        """
        ongoing_order = (
            Order.objects
            .filter(table=table, deleted=False)
            .exclude(order_status=OrderStatus_Initiated)
            .exclude(order_status=OrderStatus_Cancelled)
            .exclude(fulfilment_status='served')
            .order_by('-time_created')
            .values('id')
            .first()
        )
        if ongoing_order is not None:
            return {
                'present': True,
                'order_id': ongoing_order['id']
            }
        return {'present': False}

    @staticmethod
    def find_existing_order_item(item: dict, order_id: str):
        # Returns the matching OrderItem line (so the caller can bump it directly)
        # or None. Returning the resolved row — instead of a bare bool — is what
        # lets update_item_quantity avoid a non-unique re-lookup: the same menu
        # item can sit on an order as several lines (different modifiers/extras),
        # so an OrderItem.objects.get(order, item) would raise
        # MultipleObjectsReturned. The `existing_item` binding stays on the parent
        # line throughout (the extras loops iterate a separate `extra` variable) so
        # every match path returns that parent line, never a child-extra row.
        menu_item = MenuItem.objects.get(pk=item['item'])
        existing_items = OrderItem.objects.filter(
            order__id=order_id,
            item=menu_item,
            deleted=False
        )
        if existing_items.count() > 0:
            existing_item = existing_items[0]
            extras = item.get('extras')
            existing_item_extras = OrderItem.objects.filter(parent_item=existing_item)
            incoming_modifiers = item.get('selected_modifiers') or {}
            existing_modifiers = existing_item.selected_modifiers or {}
            # Compare on the order- and duplicate-independent SEMANTIC key, not raw
            # client JSON. The incoming selection is already canonical (normalized in
            # the order-creation transaction); keying the existing row the same way
            # keeps line-merge tolerant of any harmless legacy pre-canonical row
            # (duplicate/reordered choice ids) while genuinely different selections
            # stay distinct.
            incoming_key = ConOrder._modifier_compare_key(incoming_modifiers)
            existing_key = ConOrder._modifier_compare_key(existing_modifiers)
            has_modifiers = bool(incoming_key)

            # no extras and no options
            if existing_item_extras.count() == 0 and not has_modifiers:
                return existing_item

            # only extras but no item_options
            if existing_item_extras.count() > 0 and not has_modifiers:
                logger.debug("checking only extras with no items")
                if len(extras) == existing_item_extras.count():
                    for extra in existing_item_extras:
                        if str(extra.item.pk) not in extras:
                            return None
                    return existing_item

            # only options but no extras
            if existing_item_extras.count() == 0 and has_modifiers:
                if existing_key == incoming_key:
                    return existing_item
                return None

            # both extras and options
            if existing_item_extras.count() > 0 and has_modifiers:
                if len(extras) == existing_item_extras.count():
                    for extra in existing_item_extras:
                        if str(extra.item.pk) not in extras:
                            return None
                    if existing_key == incoming_key:
                        return existing_item

        return None

    @staticmethod
    def _modifier_compare_key(selected_modifiers) -> dict:
        """
        Build an order- and duplicate-independent semantic key for comparing two
        modifier selections when merging order lines. Incoming selections are already
        canonical (normalized in the order-creation transaction); keying the EXISTING
        row the same way keeps line-merge tolerant of any harmless legacy
        pre-canonical row (duplicate/reordered choice ids) so it still merges with the
        canonical incoming selection, while genuinely different selections stay
        distinct. Empty groups are dropped so ``{"g": []}`` compares as no selection.
        """
        return {
            str(group_id): frozenset(str(choice_id) for choice_id in (choice_ids or []))
            for group_id, choice_ids in (selected_modifiers or {}).items()
            if choice_ids
        }

    @staticmethod
    def determine_effective_unit_price(menu_item: MenuItem, selected_modifiers: dict = None) -> dict:
        # Discount activation and the effective base price come from the single,
        # timezone-aware (EAT) predicate on the model — the SAME one the diner
        # menu serializer uses — so the diner-displayed price and the charged
        # price agree. An expired / out-of-window / wrong-day / zero-value
        # discount charges primary_price even when running_discount and
        # discounted_price are set. The client still cannot inject a price: the
        # effective base is recomputed server-side from the MenuItem here.
        effective_unit_price = menu_item.effective_base_price()

        # add the cost of the grouped modifier selections
        cost_of_options = Decimal('0')
        if selected_modifiers:
            modifier_data = menu_item.options or {}
            groups_by_id = {g.get('id'): g for g in modifier_data.get('groups', [])}
            for group_id, choice_ids in selected_modifiers.items():
                group = groups_by_id.get(group_id)
                if group is None:
                    return {
                        'status': 400,
                        'message': f'Invalid modifier group for item, {menu_item.name}'
                    }
                choices_by_id = {c.get('id'): c for c in group.get('choices', [])}
                # De-dupe choice ids per group (dict.fromkeys preserves order):
                # a repeated choice must be validated and charged exactly ONCE, so
                # a duplicate cannot inflate the per-unit cost. A foreign/unknown id
                # is still caught because it survives de-duping into the set below.
                for choice_id in dict.fromkeys(choice_ids or []):
                    choice = choices_by_id.get(choice_id)
                    if choice is None:
                        return {
                            'status': 400,
                            'message': f'Invalid modifier choice for item, {menu_item.name}'
                        }
                    cost_of_options += Decimal(str(choice.get('additionalCost', 0)))

        effective_unit_price += cost_of_options
        return {
            'status': 200,
            'price': effective_unit_price.quantize(Decimal('0.01')),
            'cost_of_options': cost_of_options.quantize(Decimal('0.01'))
        }

    @staticmethod
    def process_item_extras(item: dict, order_id: str, order_item_id: str,
                            restaurant: Restaurant) -> dict:
        # `restaurant` is required and comes already-resolved from the caller
        # (add_order_item passes order.restaurant), so every extra is fetched
        # restaurant-scoped — a caller cannot forget the tenant boundary.
        # Always returns a status dict; callers must propagate any non-200.
        extras = item.get('extras', None)

        if extras is None:
            return {'status': 200}

        if not isinstance(extras, list):
            return {
                'status': 400,
                'message': NOT_ON_MENU_MESSAGE
            }

        for extra in extras:
            # defense-in-depth mirror of add_order_item's parent-item guard:
            # the scoped fetch proves ownership, and the exception tuple turns
            # a foreign / nonexistent / malformed id into a 400 dict instead
            # of an uncaught 500 (initiate_order's batch gate already rejects
            # these on the live path; this holds for any other caller).
            try:
                extra_item = MenuItem.objects.get(
                    pk=extra, section__restaurant=restaurant
                )
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                return {
                    'status': 400,
                    'message': NOT_ON_MENU_MESSAGE
                }
            unit_price = extra_item.primary_price
            quantity = 1  # extra['quantity']

            price_selection = ConOrder.determine_effective_unit_price(menu_item=extra_item)
            if price_selection.get('status') != 200:
                return price_selection

            effective_unit_price = price_selection.get('price')
            total_cost = unit_price * quantity
            discounted_cost = effective_unit_price * quantity
            savings = total_cost - discounted_cost
            actual_cost = discounted_cost

            extra = {
                'item_name': extra_item.name,
                'quantity': quantity,

                # kitchen snapshots (extras carry no modifiers)
                'item_name_snapshot': extra_item.name,
                'modifiers_snapshot': [],
                'allergen_tags_snapshot': [
                    {'name': t.name, 'icon': t.icon, 'colour': t.colour}
                    for t in extra_item.tags.filter(category='allergen')
                ],

                'unit_price': unit_price,
                'discounted_price': effective_unit_price,
                'actual_price': effective_unit_price,
                # Truthful only when the discount is actually live (same predicate
                # that set the price above), not merely when the flag is on.
                'discounted': extra_item.is_discount_active(),
                'unit_cost_of_options': 0,

                'total_cost': total_cost,
                'discounted_cost': discounted_cost,
                'savings': savings,
                'actual_cost': actual_cost,
                'cost_of_options': 0,

                'available': extra_item.available,
                'status': 'initiated'
            }

            # Mirror the parent-item guard: an out-of-stock extra (in_stock=false)
            # is zeroed and flagged unavailable so it is neither prepared nor
            # charged and is surfaced by the existing reconciliation gate.
            if not extra_item.available or not extra_item.in_stock:
                extra['quantity'] = 0
                extra['total_cost'] = 0
                extra['discounted_cost'] = 0
                extra['savings'] = 0
                extra['actual_cost'] = 0
                extra['available'] = False
                extra['status'] = 'unavailable'

            # save the item to the menu items
            extra_record = SerializerPutOrderItem(data=extra)
            if not extra_record.is_valid():
                raise Exception(extra_record.errors)
            # order / parent_item / item are server-resolved, tenant-scoped
            # objects passed through the trusted save() channel — never client
            # input (the fields are read_only on the serializer).
            extra_record.save(
                order_id=order_id,
                parent_item_id=order_item_id,
                item=extra_item,
            )

        return {'status': 200}

    @staticmethod
    def update_item_quantity(order_item, item: dict) -> dict:
        # The caller (find_existing_order_item) already resolved the exact matching
        # line, so bump it directly. Do NOT re-fetch it via
        # OrderItem.objects.get(order, item): that filter is non-unique once the
        # same menu item is on the order as more than one line and raises
        # MultipleObjectsReturned (BUG-P2-5). The recompute is left exactly as
        # before (per-unit unit_price/discounted_price scaled to the new quantity;
        # cost_of_options carried verbatim) — this is a crash-only fix, not a
        # pricing change.
        new_quantity = order_item.quantity + item['quantity']
        new_total_cost = order_item.unit_price * new_quantity
        new_cost_of_options = order_item.cost_of_options * new_quantity
        new_discounted_cost = order_item.discounted_price * new_quantity
        new_savings = new_total_cost - new_discounted_cost

        order_item.quantity = new_quantity
        order_item.total_cost = new_total_cost
        order_item.discounted_cost = new_discounted_cost
        order_item.cost_of_options = new_cost_of_options
        order_item.savings = new_savings

        order_item.save()

        return {
            'status': 200,
            'message': 'Order item quantity has been updated successfully.'
        }

    @staticmethod
    def add_order_item(item: dict, order_id: str):
        # defense-in-depth: this chokepoint self-guards for every caller. A
        # malformed payload or an item that does not belong to the order's
        # restaurant returns a 400 dict instead of raising 500 downstream.
        if (
            not isinstance(item, dict)
            or item.get('item') is None
            or item.get('quantity') is None
        ):
            return {
                'status': 400,
                'message': 'Each order item must include an item and a quantity.'
            }

        try:
            order = Order.objects.get(pk=order_id)
        except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
            return {
                'status': 400,
                'message': 'Invalid order selected'
            }

        try:
            menu_item = MenuItem.objects.get(
                pk=item['item'], section__restaurant=order.restaurant
            )
        except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
            return {
                'status': 400,
                'message': NOT_ON_MENU_MESSAGE
            }

        unit_price = menu_item.primary_price

        # check if the item already exists in the order so that we just update the quantity
        existing_item = ConOrder.find_existing_order_item(item=item, order_id=order_id)
        if existing_item is not None:
            return ConOrder.update_item_quantity(order_item=existing_item, item=item)

        # handling modifiers
        selected_modifiers = item.get('selected_modifiers') or {}
        selected_options = ConOrder.construct_option_items(item=item)

        price_selection = ConOrder.determine_effective_unit_price(
            menu_item=menu_item,
            selected_modifiers=selected_modifiers
        )
        if price_selection.get('status') != 200:
            return price_selection

        effective_unit_price = price_selection.get('price')
        unit_cost_of_options = price_selection.get('cost_of_options')

        total_cost = unit_price * item['quantity']
        discounted_cost = effective_unit_price * item['quantity']
        savings = total_cost - discounted_cost
        actual_cost = discounted_cost
        cost_of_options = unit_cost_of_options * item['quantity']

        item_data = {
            'item_name': menu_item.name,
            'quantity': item['quantity'],

            # kitchen snapshots: resolved once at creation, immutable thereafter
            'item_name_snapshot': menu_item.name,
            'modifiers_snapshot': [f"{o['name']}: {o['choices']}" for o in selected_options],
            'allergen_tags_snapshot': [
                {'name': t.name, 'icon': t.icon, 'colour': t.colour}
                for t in menu_item.tags.filter(category='allergen')
            ],

            'options': selected_options,
            'selected_modifiers': selected_modifiers,

            'unit_price': unit_price,
            'discounted_price': effective_unit_price,
            'actual_price': effective_unit_price,
            # Truthful only when the discount is actually live (same predicate
            # that set the price above), not merely when the flag is on.
            'discounted': menu_item.is_discount_active(),
            'unit_cost_of_options': unit_cost_of_options,

            'total_cost': total_cost,
            'discounted_cost': discounted_cost,
            'savings': savings,
            'actual_cost': actual_cost,
            'cost_of_options': cost_of_options,

            'available': menu_item.available,
            'status': 'initiated'
        }

        # in_stock=false (sold out) routes through the same zero-and-flag path
        # as a genuinely unavailable item: the order item's `available` flag
        # means "this line is fulfillable", not a mirror of menu_item.available.
        # Flagging it here stops it being prepared/charged and makes it count in
        # no_unavailable_items so the existing frontend reconciliation gate
        # surfaces it before submit. Do not "fix" this back to only `available`.
        if not menu_item.available or not menu_item.in_stock:
            item_data['quantity'] = 0
            item_data['total_cost'] = 0
            item_data['discounted_cost'] = 0
            item_data['savings'] = 0
            item_data['actual_cost'] = 0
            item_data['available'] = False
            item_data['status'] = 'unavailable'

        # save the item to the menu items
        item_record = SerializerPutOrderItem(data=item_data)
        if not item_record.is_valid():
            raise Exception(item_record.errors)
        # order + item are server-resolved, tenant-scoped objects passed through
        # the trusted save() channel — never accepted as client input (the fields
        # are read_only on the serializer).
        item_record.save(order=order, item=menu_item)

        # process the item extras — capture and propagate: a rejected extra
        # rejects the whole item so the service chokepoint (_create_order)
        # can abort the whole order instead of silently dropping the failure.
        extras_result = ConOrder.process_item_extras(
            item=item,
            order_id=order_id,
            order_item_id=str(item_record.data['id']),
            restaurant=order.restaurant,
        )
        if extras_result.get('status') != 200:
            return extras_result

        return {'status': 200, 'message': 'Order item added successfully.'}

    @staticmethod
    def update_order_amounts(order: Order) -> dict:
        order_items = OrderItem.objects.select_for_update().filter(
            deleted=False,
            order=order
        )
        total_cost = sum([item.total_cost for item in order_items], Decimal('0'))
        discounted_cost = sum([item.discounted_cost for item in order_items], Decimal('0'))
        savings = total_cost - discounted_cost
        actual_cost = discounted_cost

        # get the total payments done on the order
        order_payments = DinifyTransaction.objects.filter(
            order=order,
            transaction_status=TransactionStatus_Success
        )
        total_paid = order_payments.aggregate(
            Sum('transaction_amount')
        )['transaction_amount__sum'] or Decimal('0')

        balance_payable = actual_cost - total_paid

        order.total_cost = total_cost
        order.discounted_cost = discounted_cost
        order.savings = savings
        order.actual_cost = actual_cost
        order.total_paid = total_paid
        order.balance_payable = balance_payable
        order.save()

    @staticmethod
    def initiate_order(
        restaurant_id: str,
        table_id: str,
        items: list,
        customer: Union[User, None] = None,
        created_by: Union[User, None] = None,
        order_source: str = 'diner_self_service',
        client_order_id: Optional[str] = None
    ):
        try:
            restaurant = Restaurant.objects.get(pk=restaurant_id)
        except ObjectDoesNotExist:
            return {
                'status': 400,
                'message': MESSAGES.get('RESTAURANT_NOT_FOUND')
            }
        except Exception as error:
            logger.error("InitiateOrder-Error: %s", error)
            return {
                'status': 400,
                'message': MESSAGES.get('GENERAL_ERROR')
            }

        # Lifecycle admission (PREFLIGHT, FAST FEEDBACK): may this caller order at a
        # restaurant in this state at all? One rule — `order_admission.evaluate` —
        # covering both what used to be two separate gates here: whether the
        # restaurant takes orders at all (`suspended`/`offboarded` do not) and THE
        # LAUNCH BOUNDARY, which refuses the anonymous public at a restaurant that
        # has not gone live however many QR codes are already printed. The owner can
        # still place the one end-to-end rehearsal order the go-live checklist
        # requires, and that order is marked `is_test` at creation so it never
        # becomes commercial reality.
        #
        # This is preflight ONLY. It reads the instance loaded above, in autocommit,
        # so it can go stale while the request waits on a lock — the LOAD-BEARING
        # admission runs inside `_create_order`'s transaction, under the shared
        # advisory lock, against status re-read there. Same split as the menu
        # publication preflight below. Both call the SAME function, so the fast
        # answer and the authoritative one can never disagree about the rule.
        preflight_admission = evaluate(restaurant.status, created_by, STAGE_CREATE)
        if not preflight_admission.allowed:
            return {
                'status': 400,
                'message': preflight_admission.message
            }

        # availability: a diner cannot place an order while the restaurant has
        # paused ordering (accepting_orders=False). Staff/admin orders
        # (created_by set) are a management action and bypass this gate.
        if created_by is None and not restaurant.accepting_orders:
            return {
                'status': 400,
                'message': 'This restaurant is not currently accepting orders'
            }

        # check that order items are provided
        if items is None:
            return {
                'status': 400,
                'message': MESSAGES.get('NO_ORDER_ITEMS')
            }

        if len(items) < 1:
            return {
                'status': 400,
                'message': MESSAGES.get('NO_ORDER_ITEMS')
            }

        # Canonical selection validation (preflight, FAST FEEDBACK): one authority
        # for tenant ownership, diner publication (anonymous only), and extra
        # applicability — is_extra + membership in the parent's extras_applicable +
        # has_extras + no duplicate/self-reference + min/max on the validated unique
        # set. Every unorderable id (foreign / nonexistent / malformed / unpublished
        # / disallowed / wrong-role) collapses to ONE opaque NOT_ON_MENU_MESSAGE so
        # the response never reveals whether an id exists on another tenant.
        #
        # This is preflight only — the LOAD-BEARING re-check runs at a time captured
        # AFTER the table lock inside _create_order's transaction, so a menu change
        # while a request waits on that lock cannot slip a stale selection through.
        # Extras integrity applies to EVERY caller (staff included); publication is
        # gated on created_by (an authorised staff/admin order bypasses diner
        # publication but never tenant or relationship integrity). available /
        # in_stock stay OUT of this gate — they flow through the zero-and-flag
        # reconciliation in add_order_item / process_item_extras.
        # Idempotency-first: a replay of an already-created order is returned as-is
        # by _create_order even if the menu has since changed, so it must NOT be
        # re-validated here. Only a genuinely NEW submission runs the preflight;
        # the authoritative re-check still runs inside _create_order's transaction
        # (after the idempotency lookup and the table lock), which is the
        # load-bearing enforcement.
        is_replay = bool(
            client_order_id
            and Order.objects.filter(
                restaurant=restaurant, client_order_id=client_order_id,
            ).exists()
        )
        if not is_replay:
            preflight = validate_order_selections(
                restaurant, items, timezone.localtime(),
                enforce_publication=(created_by is None),
            )
            if preflight.get('status') != 200:
                return preflight

            # Modifier (option) selection limits — separate from publication.
            options_check = ConOrder.check_options_requirements(items)
            if options_check.get('status') != 200:
                return options_check

        # tenant consistency: the table must belong to this restaurant. Scoped
        # fetch validates existence AND ownership in one query, so a
        # foreign/nonexistent/malformed table id → 400 (never 500, never a
        # silent order against another restaurant's floor).
        try:
            table = Table.objects.get(pk=table_id, restaurant=restaurant)
        except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
            return {
                'status': 400,
                'message': 'Invalid table for this restaurant'
            }

        # availability: a diner may only order at a table whose QR mode permits
        # ordering. Whitelist the ordering modes so any future non-ordering mode
        # fails safe rather than accidentally permitting orders. Staff/admin
        # orders (created_by set) bypass this gate.
        ORDERING_QR_MODES = ('order_pay', 'order_only')  # 'menu_only' is view-only
        if created_by is None and table.qr_mode not in ORDERING_QR_MODES:
            return {
                'status': 400,
                'message': 'Ordering is not available at this table'
            }

        # availability: a diner cannot order at a table that is not available for
        # a scan (soft-deleted, disabled, inactive, or out of service). Reuse the
        # same predicate the diner QR-scan flow uses so the two stay consistent.
        if created_by is None and not table.is_available_for_scan():
            return {
                'status': 400,
                'message': 'This table is not available for ordering'
            }

        # idempotency, table-gating, daily numbering and creation are all
        # handled atomically by the order-creation service.
        result = _create_order(
            restaurant=restaurant,
            table=table,
            items=items,
            customer=customer,
            created_by=created_by,
            order_source=order_source,
            client_order_id=client_order_id,
        )
        if result.get('status') != 200:
            return result

        order_rec = result['order']
        order_rec.refresh_from_db()

        order_details = serialize_order_details(order=order_rec)
        return {
            'status': 200,
            'message': MESSAGES.get('ORDER_INITIATED'),
            'data': {
                'order_details': order_details.get('order'),
                'order_items': order_details.get('order_items'),
                'available_items': order_details.get('available_items'),
                'unavailable_items': order_details.get('unavailable_items'),
                'extras': order_details.get('extras'),
                'available_extras': order_details.get('available_extras'),
                'unavailable_extras': order_details.get('unavailable_extras')
            }
        }

