"""
Diner-facing order serialization.

D02 changed what some of these values MEAN internally, so this module is where the
public contract is held steady. The mapping is explicit rather than assumed:

  FIELD           LEGACY MEANING (kept on the wire)      CORRECTED INTERNAL VALUE
  unit_price      reference BASE per unit, no modifiers  reference base + modifiers
  total_cost      reference base x quantity              (base + modifiers) x qty
  discounted_cost effective (base + modifiers) x qty     unchanged
  actual_cost     payable                                unchanged (now correct)
  savings         reference - effective                  unchanged FORMULA, but the
                                                         reference now includes the
                                                         same modifiers, so it can
                                                         no longer be negative

``unit_price`` and ``total_cost`` are emitted in their LEGACY form for a CORRECTED
order — recovered exactly by subtracting the modifier component, which is what they
held before — so an older client reading them still reads the number it expects.
The corrected, modifier-inclusive figures are exposed ADDITIVELY as
``reference_unit_price`` / ``reference_total_cost``.

``savings`` is the one changed contract, and it is changed deliberately: its old
value was not a meaning but a defect. Any paid modifier made it NEGATIVE and made
an order's net exceed its gross, because the reference excluded modifiers while the
effective included them. It is documented in BREAKING_CHANGES.md rather than
preserved.
"""
from decimal import Decimal

from orders_app.models import Order, OrderItem
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED,
)
from orders_app.controllers.services.order_quote import quote_ref


def _legacy_view(item, corrected):
    """``(unit_price, total_cost, savings)`` as the established wire contract.

    For a CORRECTED order the modifier component is subtracted back out of the
    reference figures, reproducing exactly what the pre-D02 code stored. For a
    LEGACY order the stored values are already in that form and pass through.
    """
    if not corrected:
        return item.unit_price, item.total_cost, item.savings
    unit = item.unit_price - (item.unit_cost_of_options or 0)
    total = item.total_cost - item.cost_of_options
    return unit, total, item.savings


def serialize_order_details(order: Order) -> dict:
    all_order_items = OrderItem.objects.filter(order=order)
    rows = list(all_order_items)
    corrected = order.pricing_version == PRICING_VERSION_CORRECTED

    by_parent = {}
    for row in rows:
        if row.parent_item_id is not None:
            by_parent.setdefault(row.parent_item_id, []).append(row)

    # `update_order_amounts` reconciles over UNDELETED rows, so the legacy
    # total below and the quote reference are computed on the same basis.
    live_rows = [r for r in rows if not r.deleted]

    non_extra_items = [r for r in rows if r.parent_item_id is None]
    extra_items = [r for r in rows if r.parent_item_id is not None]
    parent_items = [r for r in non_extra_items if r.available]
    unavailable_parent_items = [r for r in non_extra_items if not r.available]
    unavailable_parent_ids = {r.pk for r in unavailable_parent_items}
    available_extras_selected = [r for r in extra_items if r.available]
    # P5: an extra that was zeroed only BECAUSE its parent is undeliverable is
    # not a second, separate loss — the diner lost the dish, and the extra went
    # with it. Reporting both would show one dropped selection twice.
    unavailable_extras_selected = [
        r for r in extra_items
        if not r.available and r.parent_item_id not in unavailable_parent_ids
    ]

    serialized = {
        'id': str(order.pk),
        'time_created': order.time_created,
        'order_number': order.order_number,

        'restaurant': str(order.restaurant.pk),
        'table': str(order.table.pk),
        'table_number': str(order.table.number),

        # Legacy wire meaning: the reference total EXCLUDING modifiers, which is
        # what this key held before D02. Recovered exactly by subtracting the
        # modifier component the corrected reference now carries.
        'total_cost': (
            sum((_legacy_view(r, corrected)[1] for r in live_rows), Decimal('0'))
            if corrected else order.total_cost
        ),
        'discounted_cost': order.discounted_cost,
        'savings': order.savings,
        'actual_cost': order.actual_cost,
        'prepayment_required': order.prepayment_required,

        # ADDITIVE (D02): the corrected, modifier-inclusive reference total, and
        # the acknowledgement the diner's review is bound to.
        'reference_total_cost': order.total_cost,
        'pricing_version': order.pricing_version,
        'quote_ref': quote_ref(order, rows=live_rows),

        'no_items': len(parent_items),
        'no_unavailable_items': len(unavailable_parent_items),
        'no_available_items': len(parent_items),
        'no_available_extras': len(available_extras_selected),
        'no_unavailable_extras': len(unavailable_extras_selected),
        'order_status': order.order_status,
        'payment_status': order.payment_status,
    }

    def details(item):
        return serialize_order_item_details(
            item=item, corrected=corrected,
            children=by_parent.get(item.pk, []),
        )

    return {
        'order': serialized,
        'order_items': [details(i) for i in non_extra_items],
        'available_items': [details(i) for i in parent_items],
        'unavailable_items': [details(i) for i in unavailable_parent_items],
        'extras': [details(i) for i in extra_items],
        'available_extras': [details(i) for i in available_extras_selected],
        'unavailable_extras': [details(i) for i in unavailable_extras_selected],
        # ADDITIVE (D02/P9): THE canonical review. Parent lines with their extras
        # nested underneath, with parent-only and parent-plus-extras amounts
        # explicitly distinguished so the same child can never be counted twice.
        # Derived from the saved priced rows — it repeats no arithmetic.
        'quote': [
            _quote_line(item, by_parent.get(item.pk, []), corrected)
            for item in non_extra_items
        ],
    }


def _quote_line(item, children, corrected):
    """One canonical review line: the dish, its extras, and BOTH aggregates.

    ``line_actual_cost`` is the parent alone. ``line_total_with_extras`` is the
    parent plus its extras — a DISPLAY aggregate, named so it cannot be mistaken
    for the stored payable, and never written back onto the parent row. Summing
    the parents' ``line_actual_cost`` plus every extra's own amount, or summing
    ``line_total_with_extras`` across parents, both give the order's payable;
    mixing the two would double-count, which is why they are labelled.
    """
    unit_price, total_cost, savings = _legacy_view(item, corrected)
    extras_actual = sum((child.actual_cost for child in children), Decimal('0'))
    return {
        'id': str(item.pk),
        'item': str(item.item_id),
        'item_name': item.item_name_snapshot or item.item.name,
        'quantity': item.quantity,
        'available': item.available,
        'status': item.status,
        'selected_modifiers': item.selected_modifiers or {},
        'modifiers': item.modifiers_snapshot or [],
        'options': item.options or [],

        'unit_price': unit_price,
        'reference_unit_price': item.unit_price,
        'discounted_price': item.discounted_price,
        'unit_cost_of_options': item.unit_cost_of_options or 0,
        'discounted': item.discounted,

        'total_cost': total_cost,
        'reference_total_cost': item.total_cost,
        'discounted_cost': item.discounted_cost,
        'savings': savings,
        'line_actual_cost': item.actual_cost,
        'line_total_with_extras': item.actual_cost + extras_actual,

        'extras': [
            {
                'id': str(child.pk),
                'item': str(child.item_id),
                'item_name': child.item_name_snapshot or child.item.name,
                'quantity': child.quantity,
                'available': child.available,
                'status': child.status,
                'unit_price': child.unit_price,
                'discounted_price': child.discounted_price,
                'actual_cost': child.actual_cost,
            }
            for child in children
        ],
    }


def serialize_order_item_details(item: OrderItem, corrected=None,
                                 children=None) -> dict:
    if corrected is None:
        corrected = item.order.pricing_version == PRICING_VERSION_CORRECTED
    if children is None:
        children = list(OrderItem.objects.filter(parent_item=item))
    unit_price, total_cost, savings = _legacy_view(item, corrected)
    extras_list = [
        {
            'item_name': child.item.name,
            'quantity': child.quantity,
            'actual_cost': child.actual_cost,
            'available': child.available,
            'status': child.status
        }
        for child in children
    ]
    return {
        'item': str(item.item.id),
        'item_name': item.item.name,
        'quantity': item.quantity,

        'unit_price': unit_price,
        'discounted_price': item.discounted_price,
        'discounted': item.discounted,

        'total_cost': total_cost,
        'discounted_cost': item.discounted_cost,
        'savings': savings,
        'actual_cost': item.actual_cost,

        # ADDITIVE (D02): the corrected, modifier-inclusive reference figures and
        # the line's stable identity, which is what lets a client match a server
        # line to the basket line it came from.
        'id': str(item.pk),
        'reference_unit_price': item.unit_price,
        'reference_total_cost': item.total_cost,
        'selected_modifiers': item.selected_modifiers or {},

        'available': item.available,
        'status': item.status,

        'is_extra': False if item.parent_item is None else True,
        'no_extras': len(children),
        'extras': extras_list
    }
