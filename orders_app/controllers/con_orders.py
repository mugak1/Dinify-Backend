import logging
import uuid

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
from restaurants_app.controllers.modifier_definition import (
    inspect_modifier_definition, usable_identifier,
)
from orders_app.controllers.services.order_input import (
    client_order_id_rejection, quantity_error, validate_order_items,
    validate_service_client_order_id,
)
from orders_app.controllers.services.order_admission import (
    STAGE_CREATE,
    evaluate,
)
from orders_app.controllers.services.order_pricing import (
    PricingRefused, extend, line_identity, modifier_adjustment, price_unit,
    unit_from_row,
)
from misc_app.controllers.money import MoneyConfigError, quantize_money

logger = logging.getLogger(__name__)

# One controlled diner-facing refusal for an item whose STORED modifier
# definition is malformed. It says what the diner can act on and nothing about
# the catalogue; the classification and the offending identifier go to the log.
MODIFIER_CONFIG_MESSAGE = (
    'Item {name} cannot be ordered right now. Please choose another item.'
)

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

        # ONE structural reading of the stored definition, shared with the
        # read-only preflight command so the two can never disagree about what
        # a catalogue row means.
        definition = inspect_modifier_definition(menu_item.options)

        if definition.is_invalid:
            # FAIL CLOSED. A malformed ACTIVE definition is refused, never
            # downgraded to "no modifiers required" — that would delete a
            # required selection to make a broken item orderable. Log a bounded
            # classification and the offending identifier only; never the
            # catalogue JSON.
            logger.warning(
                'Malformed modifier definition refused at checkout '
                '(menu_item_id=%s, reason=%s, group_id=%s)',
                menu_item.pk, definition.reason,
                ConOrder._safe_identifier_for_log(definition.group_id),
            )
            return {
                'status': 400,
                'message': MODIFIER_CONFIG_MESSAGE.format(name=menu_item.name),
            }

        # Item with no active modifiers: an absent/null/empty selection normalizes
        # to {}, but a non-empty modifier object is rejected rather than silently
        # persisted as unrelated client data.
        if not definition.is_active:
            if not selected_modifiers:
                return {'status': 200, 'selected_modifiers': {}}
            return {
                'status': 400,
                'message': f'Item {menu_item.name} does not accept modifier selections.'
            }

        # Unique by construction: duplicate group ids are refused above.
        groups_by_id = {group.group_id: group for group in definition.groups}

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
        for group in definition.groups:
            submitted = selected_modifiers.get(group.group_id, [])
            if group.group_id in selected_modifiers and not isinstance(submitted, list):
                return {
                    'status': 400,
                    'message': f'Invalid modifier selection for item, {menu_item.name}'
                }
            choice_ids = submitted if isinstance(submitted, list) else []

            # Every submitted member must be usable AS an identifier BEFORE it
            # reaches a hashing operation. This is the guard that stops a nested
            # object or list raising out of the de-duplication below.
            if any(not usable_identifier(choice) for choice in choice_ids):
                return {
                    'status': 400,
                    'message': f'Invalid modifier choice for item, {menu_item.name}'
                }

            defined_choice_ids = group.choice_ids
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
            selected_count = len(canonical_choice_ids)
            if selected_count < group.min_selections:
                return {
                    'status': 400,
                    'message': f"Item {menu_item.name} requires at least {group.min_selections} option selections."
                }
            if group.max_selections and selected_count > group.max_selections:
                return {
                    'status': 400,
                    'message': f"Item {menu_item.name} allows a maximum of {group.max_selections} option selections."
                }

            # Omit empty optional groups from the canonical form.
            if canonical_choice_ids:
                canonical[group.group_id] = canonical_choice_ids

        return {'status': 200, 'selected_modifiers': canonical}

    @staticmethod
    def _safe_identifier_for_log(value):
        """Bounded, printable rendering of an operator-supplied identifier for a
        log line. Never the catalogue JSON."""
        if value is None:
            return None
        text = value if isinstance(value, str) else repr(value)
        return text[:64]

    @staticmethod
    def normalize_order_items(restaurant, order_items: list, snapshot=None) -> dict:
        """
        Batch counterpart of ``normalize_selected_modifiers`` for the authoritative
        order-creation transaction. For each line it resolves the MenuItem
        tenant-scoped, canonicalizes its ``selected_modifiers``, and returns a NEW list
        of shallow-copied items whose ``selected_modifiers`` is the canonical form —
        the caller's original items are never mutated in place.

        ``snapshot`` is the order's single coherent catalogue read (D02). When it is
        supplied this issues NO query of its own: it was one of THREE separate reads
        of the same rows inside the transaction, and under READ COMMITTED each of
        those took its own database snapshot, so validation, canonicalisation and
        pricing could each see a different committed version of one row. The
        restaurant-scoped fallback is preserved for every other caller.

        Returns ``{'status': 200, 'items': [<normalized copies>]}`` or the first
        controlled ``{'status': 400, 'message': ...}`` rejection.
        """
        wanted = []
        for item in order_items:
            if not isinstance(item, dict) or item.get('item') is None:
                return {
                    'status': 400,
                    'message': 'Each order item must include an item and a quantity.'
                }
            try:
                wanted.append(uuid.UUID(str(item['item'])))
            except (ValueError, TypeError):
                return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}

        if snapshot is None:
            fetched = {
                menu_item.pk: menu_item
                for menu_item in MenuItem.objects.filter(
                    pk__in=set(wanted), section__restaurant=restaurant,
                )
            }
        else:
            fetched = {
                item_id: snapshot.menu_item(item_id)
                for item_id in set(wanted)
                if snapshot.menu_item(item_id) is not None
            }

        normalized = []
        for item, item_id in zip(order_items, wanted):
            menu_item = fetched.get(item_id)
            if menu_item is None:
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
    def option_breakdown(menu_item, selected_modifiers):
        """ONE traversal of a line's modifier selection (D02 §4.2).

        Canonicalises each selected choice's adjustment EXACTLY ONCE and returns
        both the canonical adjustments (which price the line) and the display
        options (which label it), so the cost a diner is shown for a group and
        the cost they are charged for it are the same number by construction.
        Before this they were computed separately — the label path summed into a
        ``float``, the pricing path into a ``Decimal`` — and a repeated or
        malformed value could make them disagree.

        Returns ``{'status': 200, 'adjustments': [...], 'options': [...]}`` or a
        controlled 400. Never raises on stored data.
        """
        selected_modifiers = selected_modifiers or {}
        definition = inspect_modifier_definition(menu_item.options)
        if definition.is_invalid:
            return {
                'status': 400,
                'message': MODIFIER_CONFIG_MESSAGE.format(name=menu_item.name),
            }
        if not definition.is_active or not selected_modifiers:
            return {'status': 200, 'adjustments': [], 'options': []}

        groups_by_id = {group.group_id: group for group in definition.groups}
        adjustments = []
        options = []
        for group_id, choice_ids in selected_modifiers.items():
            group = groups_by_id.get(group_id)
            if group is None:
                return {
                    'status': 400,
                    'message': f'Invalid modifier group for item, {menu_item.name}'
                }
            # An unusable member is REFUSED, never dropped: silently ignoring it
            # would price a selection the diner did not make. Checked before the
            # hash the de-duplication below would otherwise raise on.
            if any(not usable_identifier(c) for c in (choice_ids or [])):
                return {
                    'status': 400,
                    'message': f'Invalid modifier choice for item, {menu_item.name}'
                }
            # De-dupe per group: a repeated choice is validated, labelled and
            # charged exactly once.
            resolved_choices = []
            for choice_id in dict.fromkeys(choice_ids or []):
                choice = group.choices_by_id.get(choice_id)
                if choice is None:
                    return {
                        'status': 400,
                        'message': f'Invalid modifier choice for item, {menu_item.name}'
                    }
                resolved_choices.append(choice)

            group_total = Decimal('0')
            for choice in resolved_choices:
                try:
                    adjustment = modifier_adjustment(choice.get('additionalCost', 0))
                except PricingRefused as refused:
                    # A malformed stored adjustment used to raise
                    # decimal.InvalidOperation straight out of checkout as a 500
                    # ('abc', None, True, [], {}, 'NaN', 'Infinity', '1e400' all
                    # did). It is now a controlled refusal, and it is NEVER
                    # defaulted to zero — that would make a paid option free.
                    logger.warning(
                        'Unreadable modifier cost refused at checkout '
                        '(menu_item_id=%s, reason=%s, group_id=%s)',
                        menu_item.pk, refused.detail,
                        ConOrder._safe_identifier_for_log(group_id),
                    )
                    return {
                        'status': 400,
                        'message': MODIFIER_CONFIG_MESSAGE.format(
                            name=menu_item.name),
                    }
                adjustments.append(adjustment)
                group_total += adjustment

            if not resolved_choices:
                continue
            group_total = quantize_money(group_total)
            options.append({
                'name': group.raw.get('name'),
                # LEGACY SHAPE, DELIBERATELY KEPT: `cost` stays a JSON number so
                # any existing reader of this persisted blob is unaffected. It is
                # now derived from an already-canonical 2dp Decimal rather than
                # summed as floats, so it can no longer carry a binary artefact.
                # Nothing prices from it.
                'cost': float(group_total),
                # ADDITIVE and canonical: the exact decimal string. This is the
                # value to read; `cost` is compatibility.
                'cost_amount': str(group_total),
                'choices': ', '.join(c.get('name', '') for c in resolved_choices),
            })
        return {'status': 200, 'adjustments': adjustments, 'options': options}

    @staticmethod
    def construct_option_items(item: dict, menu_item=None) -> list:
        """Display options for one line. Thin wrapper over ``option_breakdown``
        so labels and pricing can never come from two traversals.

        Kept for its existing callers; it returns ``[]` for a configuration the
        breakdown refuses, because a label list has no way to report one. The
        pricing path calls ``option_breakdown`` directly and DOES fail closed.
        """
        if menu_item is None:
            menu_item = MenuItem.objects.get(pk=item['item'])
        breakdown = ConOrder.option_breakdown(
            menu_item, item.get('selected_modifiers') or {},
        )
        if breakdown.get('status') != 200:
            return []
        return breakdown['options']

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
    def line_identity_for(menu_item, item, unit, deliverable, name_snapshot,
                          option_labels):
        """The canonical D03 identity of the parent line this request describes."""
        return line_identity(
            item_id=menu_item.pk,
            selected_modifiers=item.get('selected_modifiers') or {},
            extra_ids=item.get('extras') or [],
            reference_unit=unit.reference_unit,
            effective_unit=unit.effective_unit,
            deliverable=deliverable,
            name_snapshot=name_snapshot,
            modifiers_snapshot=option_labels,
        )

    @staticmethod
    def row_identity(row, children=None):
        """The canonical identity of a PERSISTED parent line.

        Computed from the row's own stored values, so an existing line and an
        incoming request are compared on exactly the same key.
        """
        if children is None:
            children = list(
                OrderItem.objects.filter(parent_item=row, deleted=False)
            )
        return line_identity(
            item_id=row.item_id,
            selected_modifiers=row.selected_modifiers or {},
            extra_ids=[child.item_id for child in children],
            reference_unit=row.unit_price,
            effective_unit=row.discounted_price,
            deliverable=bool(row.available),
            name_snapshot=row.item_name_snapshot,
            modifiers_snapshot=row.modifiers_snapshot or [],
        )

    @staticmethod
    def find_existing_order_item(item: dict, order_id: str, menu_item=None,
                                 identity=None):
        """Find the line on this order that this request is the SAME line as.

        D03. Four defects lived in the version this replaces, and each one is
        closed by construction here rather than by another branch:

        * **Absence was a wildcard.** ``has_modifiers`` was derived from the
          INCOMING selection only, so the first branch returned on ``not
          has_modifiers`` without comparing anything: a plain dish merged into a
          modified one (and the diner was served two of the modified version),
          while the reverse order correctly produced two lines. Identity is now
          symmetric — an empty selection matches only another empty selection.
        * **``.first()`` examined ONE candidate.** Submitting A, B, A left three
          lines because the newest candidate (B) was compared and the match (A)
          never looked at. Every undeleted candidate is now compared.
        * **Child-extra rows were candidates.** The filter had no
          ``parent_item__isnull=True``, and ``Meta.ordering`` is ``-time_created``
          — so a dish that is ALSO an extra of an earlier line matched its own
          child row, and an independent top-level dish was merged into an extra.
        * **``len(extras)`` on ``None``.** An incoming line with no extras,
          against an existing line that had some, raised ``TypeError`` — an
          uncaught HTTP 500. There is no length comparison left to raise.

        ``identity`` is passed by ``add_order_item``, which has already computed
        it. The fallback recomputes the request's identity so any other caller
        gets the same rule.
        """
        if menu_item is None:
            menu_item = MenuItem.objects.get(pk=item['item'])
        if identity is None:
            # The request's OWN selections must be priced here — an identity
            # computed without them would describe a different line.
            resolved = ConOrder._resolve_for_pricing(
                menu_item, item.get('selected_modifiers') or {},
            )
            if resolved.get('status') != 200:
                return None
            identity = ConOrder.line_identity_for(
                menu_item, item, resolved['unit'], resolved['deliverable'],
                menu_item.name,
                [f"{o['name']}: {o['choices']}" for o in resolved['options']],
            )

        candidates = list(
            OrderItem.objects.filter(
                order__id=order_id,
                item=menu_item,
                deleted=False,
                # PARENT ROWS ONLY. A top-level dish must never merge into a
                # child-extra row, and an extra row must never be bumped by an
                # unrelated top-level line.
                parent_item__isnull=True,
            )
        )
        if not candidates:
            return None
        children_by_parent = {}
        for child in OrderItem.objects.filter(
            parent_item__in=candidates, deleted=False,
        ):
            children_by_parent.setdefault(child.parent_item_id, []).append(child)

        for candidate in candidates:
            if ConOrder.row_identity(
                candidate, children_by_parent.get(candidate.pk, []),
            ) == identity:
                return candidate
        return None

    @staticmethod
    def _resolve_for_pricing(menu_item, selected_modifiers=None, now=None,
                             verdict=None):
        """Price ONE line's units from a resolved catalogue row.

        Returns ``{'status': 200, 'unit': PricedUnit, 'options': [...],
        'deliverable': bool}`` or a controlled 400.
        """
        if verdict is None:
            verdict = menu_item.price_verdict(now)
        if not verdict.usable:
            logger.warning(
                'Unpriceable item refused at checkout (menu_item_id=%s, reason=%s)',
                menu_item.pk, verdict.reason,
            )
            return {
                'status': 400,
                'message': MODIFIER_CONFIG_MESSAGE.format(name=menu_item.name),
            }
        breakdown = ConOrder.option_breakdown(menu_item, selected_modifiers or {})
        if breakdown.get('status') != 200:
            return breakdown
        try:
            unit = price_unit(verdict, breakdown['adjustments'])
        except PricingRefused as refused:
            logger.warning(
                'Refused line pricing (menu_item_id=%s, code=%s)',
                menu_item.pk, refused.code,
            )
            return {
                'status': 400,
                'message': MODIFIER_CONFIG_MESSAGE.format(name=menu_item.name),
            }
        return {
            'status': 200,
            'unit': unit,
            'options': breakdown['options'],
            'deliverable': bool(menu_item.available and menu_item.in_stock),
        }

    @staticmethod
    def determine_effective_unit_price(menu_item: MenuItem,
                                       selected_modifiers: dict = None,
                                       now=None, verdict=None) -> dict:
        """Effective per-unit price for one line.

        Keeps its established keys (``price``, ``cost_of_options``) and adds
        ``reference_price`` and ``unit``. Discount activation and the effective
        base come from the ONE shared price policy, so the diner-displayed price
        and the charged price cannot diverge; the modifier adjustments come from
        the ONE breakdown that also produced the labels. The client still cannot
        inject a price — nothing here reads a client amount.
        """
        resolved = ConOrder._resolve_for_pricing(
            menu_item, selected_modifiers, now=now, verdict=verdict,
        )
        if resolved.get('status') != 200:
            return resolved
        unit = resolved['unit']
        return {
            'status': 200,
            'price': unit.effective_unit,
            'reference_price': unit.reference_unit,
            'cost_of_options': unit.modifier_unit,
            'unit': unit,
        }

    @staticmethod
    def extended_values(unit, quantity) -> dict:
        """Every monetary field of one line, from immutable units and a quantity.

        The ONE place a line's money is computed, used by both the creation path
        (through the serializer) and the recalculation path. Every field moves
        TOGETHER — the pre-fix merge updated five of them and left
        ``actual_cost`` holding its pre-merge value, which is the figure the
        diner's order-detail read renders and the figure the Popular Items and
        Menu-performance reports aggregate.
        """
        line = extend(unit, quantity)
        return {
            'quantity': line.quantity,
            'unit_price': line.reference_unit,
            'discounted_price': line.effective_unit,
            'unit_cost_of_options': line.modifier_unit,
            'total_cost': line.total_cost,
            'discounted_cost': line.discounted_cost,
            'cost_of_options': line.cost_of_options,
            'savings': line.savings,
            'actual_cost': line.actual_cost,
        }

    @staticmethod
    def _extend_row(row, unit, quantity):
        """Apply :meth:`extended_values` to a persisted row, in place."""
        values = ConOrder.extended_values(unit, quantity)
        for field, value in values.items():
            setattr(row, field, value)
        return values

    @staticmethod
    def rebuild_line(order_item, new_quantity, children=None):
        """Recompute a persisted parent line, and its extras, for a new quantity.

        RECALCULATION, not reinterpretation: every amount comes from the row's
        own saved canonical UNIT components (``unit_from_row``) multiplied by the
        new quantity. The pre-fix path did ``order_item.cost_of_options *
        new_quantity`` on an ALREADY-EXTENDED value, so merging 2 + 1 charged
        9 000 of options where 4 500 were due, and it never touched
        ``actual_cost`` at all.

        A line that is not deliverable STAYS at quantity 0 with zero amounts. The
        pre-fix merge added the incoming quantity to the stored 0 and multiplied
        the unit price by it, so a sold-out dish submitted twice came back
        payable — flagged unavailable and charged 10 000 at the same time.

        Children are rescaled to the parent's quantity (one selected extra per
        dish) from their own saved units, so a merge can neither drop an extra
        nor leave one at the quantity it was first written with.
        """
        deliverable = bool(order_item.available)
        effective_quantity = new_quantity if deliverable else 0
        try:
            ConOrder._extend_row(
                order_item, unit_from_row(order_item), effective_quantity,
            )
        except (PricingRefused, MoneyConfigError):
            return {
                'status': 400,
                'message': 'This item cannot be ordered right now. '
                           'Please choose another item.',
            }
        order_item.save()

        if children is None:
            children = list(
                OrderItem.objects.filter(parent_item=order_item, deleted=False)
            )
        for child in children:
            child_deliverable = bool(child.available) and deliverable
            try:
                ConOrder._extend_row(
                    child, unit_from_row(child),
                    effective_quantity if child_deliverable else 0,
                )
            except (PricingRefused, MoneyConfigError):
                return {
                    'status': 400,
                    'message': 'This item cannot be ordered right now. '
                               'Please choose another item.',
                }
            child.save()
        return {
            'status': 200,
            'message': 'Order item quantity has been updated successfully.'
        }

    @staticmethod
    def update_item_quantity(order_item, item: dict) -> dict:
        """Increase a matched line's quantity by the incoming line's quantity.

        SELF-GUARDING through the SHARED D01 rule: this helper takes a
        client-supplied increment into arithmetic and then into an UPDATE, and
        the database constraint cannot stand in for the check — an existing 5
        plus an incoming -1 yields 4, which satisfies ``quantity >= 0`` perfectly
        while halving what the diner is charged.

        The per-line ceiling bounds the INCREMENT, never the merged row: two
        legitimate lines of 60 may merge to 120, bounded order-wide by
        MAX_TOTAL_UNITS at the request boundary.
        """
        if quantity_error(item.get('quantity') if isinstance(item, dict) else None):
            return {
                'status': 400,
                'message': 'Each item must include a valid quantity.',
            }
        return ConOrder.rebuild_line(
            order_item, order_item.quantity + item['quantity'],
        )

    @staticmethod
    def process_item_extras(item: dict, order_id: str, order_item_id: str,
                            restaurant: Restaurant, parent_quantity: int = 1,
                            parent_deliverable: bool = True, snapshot=None,
                            now=None) -> dict:
        """Persist one line's selected extras, each at the parent's quantity.

        D02 (P1): an extra is ONE PER UNIT OF ITS PARENT DISH — the rule the
        diner app's basket arithmetic has always used. It was hardcoded
        ``quantity = 1`` here, so three burgers with cheese were charged, and
        prepared, with one cheese.

        D02 (P5): when the PARENT is not deliverable its extras are zeroed and
        flagged too. They were previously priced on their own availability
        alone, so a sold-out dish left its cheese payable and on the kitchen
        board with no dish to put it on.

        Returns ``{'status': 200, 'deliverable_extras': n}`` or a controlled 400
        the caller must propagate.
        """
        extras = item.get('extras', None)
        if extras is None:
            return {'status': 200, 'deliverable_extras': 0, 'rows': []}
        if not isinstance(extras, list):
            return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}

        deliverable_extras = 0
        created_rows = []
        for extra_id in extras:
            resolved = snapshot.get(extra_id) if snapshot is not None else None
            if resolved is not None:
                extra_item = resolved.menu_item
                verdict = resolved.verdict
                allergen_tags = resolved.allergen_tags
            else:
                # Fallback for any caller without a snapshot: the scoped fetch
                # proves tenant ownership, and the exception tuple turns a
                # foreign / nonexistent / malformed id into a 400 rather than an
                # uncaught 500.
                try:
                    extra_item = MenuItem.objects.get(
                        pk=extra_id, section__restaurant=restaurant,
                    )
                except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                    return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
                verdict = extra_item.price_verdict(now)
                allergen_tags = [
                    {'name': t.name, 'icon': t.icon, 'colour': t.colour}
                    for t in extra_item.tags.filter(category='allergen')
                ]

            priced = ConOrder._resolve_for_pricing(
                extra_item, None, now=now, verdict=verdict,
            )
            if priced.get('status') != 200:
                return priced

            extra_deliverable = priced['deliverable'] and parent_deliverable
            if extra_deliverable:
                deliverable_extras += 1

            try:
                amounts = ConOrder.extended_values(
                    priced['unit'],
                    parent_quantity if extra_deliverable else 0,
                )
            except (PricingRefused, MoneyConfigError):
                return {
                    'status': 400,
                    'message': MODIFIER_CONFIG_MESSAGE.format(name=extra_item.name),
                }

            extra_data = {
                'item_name': extra_item.name,
                # kitchen snapshots (extras carry no modifiers)
                'item_name_snapshot': extra_item.name,
                'modifiers_snapshot': [],
                'allergen_tags_snapshot': allergen_tags,
                'options': [],
                'selected_modifiers': {},
                'discounted': priced['unit'].discount_active,
                'available': extra_deliverable,
                'status': 'initiated' if extra_deliverable else 'unavailable',
                **amounts,
            }
            extra_record = SerializerPutOrderItem(data=extra_data)
            if not extra_record.is_valid():
                raise Exception(extra_record.errors)
            # order / parent_item / item are server-resolved, tenant-scoped
            # objects passed through the trusted save() channel — never client
            # input (the fields are read_only on the serializer).
            created_rows.append(extra_record.save(
                order_id=order_id,
                parent_item_id=order_item_id,
                item=extra_item,
            ))

        # The caller gets the rows back so it never has to re-SELECT the children
        # it just wrote — that re-read was one query PER LINE.
        return {
            'status': 200,
            'deliverable_extras': deliverable_extras,
            'rows': created_rows,
        }

    @staticmethod
    def add_order_item(item: dict, order_id: str, order: Order = None,
                       snapshot=None, index=None):
        """Add ONE submitted line to an order, merging it into the line it is
        genuinely identical to.

        This chokepoint SELF-GUARDS for every caller — shape, quantity, tenant
        ownership and priceability are all checked here, so a caller that skips
        the endpoint cannot bypass them. ``snapshot`` and ``index`` are
        optimisations, never trust boundaries: without them the same rules run
        against a scoped query and a bounded database lookup.
        """
        if (
            not isinstance(item, dict)
            or item.get('item') is None
            or quantity_error(item.get('quantity')) is not None
        ):
            return {
                'status': 400,
                'message': 'Each order item must include an item and a quantity.'
            }

        if order is None:
            try:
                order = Order.objects.get(pk=order_id)
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                return {'status': 400, 'message': 'Invalid order selected'}

        now = snapshot.now if snapshot is not None else None
        resolved_row = snapshot.get(item['item']) if snapshot is not None else None
        if resolved_row is not None:
            menu_item = resolved_row.menu_item
            verdict = resolved_row.verdict
            allergen_tags = resolved_row.allergen_tags
        else:
            try:
                menu_item = MenuItem.objects.get(
                    pk=item['item'], section__restaurant=order.restaurant,
                )
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                return {'status': 400, 'message': NOT_ON_MENU_MESSAGE}
            verdict = menu_item.price_verdict(now)
            allergen_tags = [
                {'name': t.name, 'icon': t.icon, 'colour': t.colour}
                for t in menu_item.tags.filter(category='allergen')
            ]

        priced = ConOrder._resolve_for_pricing(
            menu_item, item.get('selected_modifiers') or {}, now=now,
            verdict=verdict,
        )
        if priced.get('status') != 200:
            return priced

        unit = priced['unit']
        selected_options = priced['options']
        deliverable = priced['deliverable']
        modifiers_snapshot = [f"{o['name']}: {o['choices']}" for o in selected_options]

        identity = ConOrder.line_identity_for(
            menu_item, item, unit, deliverable, menu_item.name,
            modifiers_snapshot,
        )

        existing_item = (
            index.get(identity) if index is not None
            else ConOrder.find_existing_order_item(
                item=item, order_id=order_id, menu_item=menu_item,
                identity=identity,
            )
        )
        if existing_item is not None:
            return ConOrder.update_item_quantity(
                order_item=existing_item, item=item,
            )

        try:
            # A line whose item is unavailable or sold out is written at
            # quantity 0 with zero amounts: neither prepared nor charged, while
            # its unit and name snapshots survive for the diner's
            # reconciliation. `available` means "this LINE is fulfillable", not
            # a mirror of menu_item.available.
            amounts = ConOrder.extended_values(
                unit, item['quantity'] if deliverable else 0,
            )
        except (PricingRefused, MoneyConfigError):
            return {
                'status': 400,
                'message': MODIFIER_CONFIG_MESSAGE.format(name=menu_item.name),
            }

        item_data = {
            'item_name': menu_item.name,
            # kitchen snapshots: resolved once at creation, immutable thereafter
            'item_name_snapshot': menu_item.name,
            'modifiers_snapshot': modifiers_snapshot,
            'allergen_tags_snapshot': allergen_tags,
            'options': selected_options,
            'selected_modifiers': item.get('selected_modifiers') or {},
            'discounted': unit.discount_active,
            'available': deliverable,
            'status': 'initiated' if deliverable else 'unavailable',
            **amounts,
        }
        item_record = SerializerPutOrderItem(data=item_data)
        if not item_record.is_valid():
            raise Exception(item_record.errors)
        # order + item are server-resolved, tenant-scoped objects passed through
        # the trusted save() channel — never accepted as client input (the fields
        # are read_only on the serializer).
        row = item_record.save(order=order, item=menu_item)

        extras_result = ConOrder.process_item_extras(
            item=item,
            order_id=order_id,
            order_item_id=str(row.pk),
            restaurant=order.restaurant,
            parent_quantity=row.quantity,
            parent_deliverable=deliverable,
            snapshot=snapshot,
            now=now,
        )
        if extras_result.get('status') != 200:
            return extras_result

        # P4: a dish whose REQUIRED extras minimum cannot be met by the extras
        # that actually survived is not deliverable. It counts the surviving
        # ELIGIBLE CHOSEN extras — one unavailable extra does not condemn a dish
        # whose remaining selections still satisfy the minimum — and it never
        # substitutes an extra the diner did not choose. Before this the
        # publication gate counted SUBMITTED extras, so a dish with a required
        # sauce that had just sold out shipped available, payable and
        # unmakeable.
        children = extras_result.get('rows') or []

        minimum = menu_item.extras_min_selections or 0
        if deliverable and menu_item.has_extras and minimum > 0:
            if extras_result.get('deliverable_extras', 0) < minimum:
                unmet = ConOrder._mark_line_undeliverable(row, children=children)
                if unmet.get('status') != 200:
                    return unmet
                deliverable = False

        if index is not None:
            # Re-key on the row as persisted: a line that has just been flipped
            # undeliverable is no longer the same line as a deliverable one, and
            # P3 keeps those apart. The children are the rows just written, so
            # keying costs no query.
            index[ConOrder.row_identity(row, children)] = row
        return {'status': 200, 'message': 'Order item added successfully.'}

    @staticmethod
    def _mark_line_undeliverable(row, children=None):
        """Zero a parent line and everything that depends on it.

        P5: dependent extras are zeroed and flagged with the parent, so an
        undeliverable dish can never leave a payable, preparable extra behind.
        The diner's reconciliation presents this as ONE loss — the dish — rather
        than the dish and each of its extras separately; that partition lives in
        ``serialize_order_details``.
        """
        row.available = False
        row.status = 'unavailable'
        try:
            ConOrder._extend_row(row, unit_from_row(row), 0)
        except (PricingRefused, MoneyConfigError):
            return {
                'status': 400,
                'message': 'This item cannot be ordered right now. '
                           'Please choose another item.',
            }
        row.save()
        if children is None:
            children = list(
                OrderItem.objects.filter(parent_item=row, deleted=False)
            )
        for child in children:
            child.available = False
            child.status = 'unavailable'
            try:
                ConOrder._extend_row(child, unit_from_row(child), 0)
            except (PricingRefused, MoneyConfigError):
                return {
                    'status': 400,
                    'message': 'This item cannot be ordered right now. '
                               'Please choose another item.',
                }
            child.save()
        return {'status': 200}

    @staticmethod
    def update_order_amounts(order: Order) -> dict:
        """Reconcile the order from its persisted rows.

        EVERY PAYABLE COMPONENT IS COUNTED EXACTLY ONCE: a parent row's amounts
        cover the dish and its modifiers, each extra is its own row, and the sum
        walks the rows. A parent's amounts deliberately do NOT include its
        extras, so there is no path on which a child is added twice — the
        parent-plus-extras figure a diner sees is derived for display and
        labelled as such, never stored on the parent.

        ``savings`` is comparable reference minus comparable effective and is
        non-negative by construction. Before this the reference excluded
        modifiers while the effective included them, so any paid modifier
        produced a NEGATIVE order-level saving and a net that exceeded its gross.
        """
        order_items = OrderItem.objects.select_for_update().filter(
            deleted=False,
            order=order
        )
        total_cost = sum([item.total_cost for item in order_items], Decimal('0'))
        discounted_cost = sum([item.discounted_cost for item in order_items], Decimal('0'))
        savings = total_cost - discounted_cost
        actual_cost = discounted_cost

        # Payment state stays derived from verified transactions. Nothing here
        # marks an order paid, collected, settled or refunded.
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
    def deliverable_parent_count(order) -> int:
        """Parent lines that will actually reach the kitchen.

        Deliverability AND quantity, deliberately NOT a payable amount: a
        legitimately free dish (0.00) is orderable and must still count.
        """
        return OrderItem.objects.filter(
            order=order, deleted=False, parent_item__isnull=True,
            available=True, quantity__gt=0,
        ).count()

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

        # STATIC INPUT VALIDATION (D01). One pure, database-free rule for the
        # request's SHAPE: a non-empty list of object lines, each with a real
        # UUID item id and a real positive integer quantity inside the request
        # ceilings, with structurally sound nested selections. It replaces the
        # old `items is None` / `len(items) < 1` pair, which let `items` be any
        # sized object (`len(5)` raised) and checked a quantity only for
        # presence — so 0, -3, 2.0, "3", True and a nested object all travelled
        # on into Decimal arithmetic and a persisted row.
        #
        # It runs BEFORE the replay lookup below, deliberately: these checks are
        # static and menu-independent, so they cannot be invalidated by anything
        # that happens to the catalogue, and a body that is not a well-formed
        # order is not a well-formed order whether or not it repeats a key. A
        # CORRECTLY SHAPED replay is unaffected — it reaches the lookup exactly
        # as before, and is still never re-validated against mutable menu state.
        # Request forms that are newly invalid or over the ceilings are
        # intentionally refused; no compatibility is promised for a previously
        # accepted malformed body.
        #
        # The VALIDATED lines are consumed downstream — raw input is not passed
        # onward beside them.
        static_input = validate_order_items(items)
        if static_input.get('status') != 200:
            return static_input
        items = static_input['items']

        # The OPTIONAL IDEMPOTENCY KEY is validated here too — before the replay
        # lookup below, which is the first thing that consumes it. The endpoint
        # already validates it, but this service is a supported entrypoint in
        # its own right and must not depend on its caller having done so: an
        # unvalidated key reached an ORM filter on a `UUIDField`, where a
        # malformed value raised `ValidationError` out of the service and an
        # integer or boolean was COERCED by `uuid.UUID(int=...)` into a
        # fabricated key (0 and False both becoming the nil UUID).
        #
        # The canonical STRING is what the rest of this function uses, and it is
        # what is handed to `_create_order` — the raw argument is not carried on
        # beside it.
        client_order_id, key_ok = validate_service_client_order_id(client_order_id)
        if not key_ok:
            return client_order_id_rejection()

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
            client_order_id is not None
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
                'unavailable_extras': order_details.get('unavailable_extras'),
                # THE authoritative quote the diner confirms. Without it the
                # review screen has nothing to show but the client's own
                # arithmetic, which is the whole defect this path closes — so
                # forward it here rather than leaving the serializer's work
                # stranded one layer down.
                'quote': order_details.get('quote'),
            }
        }
