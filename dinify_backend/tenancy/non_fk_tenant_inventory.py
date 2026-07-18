"""
Non-FK tenant-reference inventory (TENANT-STRUCT-00).

The FK meta-test (``tests_relation_classification``) reasons ONLY about DRF
``ModelSerializer`` relational fields. A large class of tenant-relevant identifiers
live OUTSIDE that lens and CANNOT be proven by it:

  * UUIDs embedded in JSON fields (no FK integrity, no serializer relation);
  * raw UUID arrays;
  * denormalised restaurant ids / snapshots copied onto rows;
  * direct ``Model.objects.create/update`` in controllers that bypass serializers;
  * dynamic model dispatch (the model is chosen at runtime);
  * cache / idempotency keys scoped by ``(restaurant, ...)``.

This module is that inventory: a machine-readable, reviewable AUDIT LIST — NOT a
proof of safety and NOT a remediation. Each entry names WHERE the identifier lives,
its KIND, a domain OWNER, the follow-up DOMAIN, and a STATUS. ``status`` is
``pending-audit`` for everything seeded here: presence in this list asserts only
"a human should confirm this path scopes by tenant", never that it does. Do not
treat ``len(INVENTORY)`` as a vulnerability count. Grow this list (and flip entries
to ``audited`` / ``remediated`` in follow-up PRs) as the audit proceeds; the
accompanying meta-test only checks the list is well-formed.
"""

# Permitted ``kind`` values — the taxonomy of non-FK tenant references.
KINDS = frozenset({
    "json-uuid",          # a tenant UUID stored inside a JSONField value
    "uuid-array",         # a raw list of UUIDs (no FK integrity)
    "denormalised-id",    # a restaurant id / snapshot copied onto a row
    "direct-write",       # Model.objects.create/update bypassing a serializer
    "dynamic-dispatch",   # the model/serializer is resolved at runtime
    "cache-idempotency-key",  # a (restaurant, ...) scoped key
})

STATUSES = frozenset({"pending-audit", "audited", "remediated"})

REQUIRED_KEYS = frozenset({
    "identifier", "location", "kind", "owner", "follow_up_domain", "status", "note",
})


INVENTORY = [
    # --- UUIDs inside JSON fields ---------------------------------------------
    {
        "identifier": "MenuItem.options[].id / options[].choices[].id",
        "location": "restaurants_app/models.py:343",
        "kind": "json-uuid",
        "owner": "menu",
        "follow_up_domain": "menu-config",
        "status": "pending-audit",
        "note": "Grouped-modifier group/choice UUIDs live in a JSONField; no FK. An "
                "order references them by value (see OrderItem.selected_modifiers).",
    },
    {
        "identifier": "MenuItem.discount_details",
        "location": "restaurants_app/models.py:323",
        "kind": "json-uuid",
        "owner": "menu",
        "follow_up_domain": "menu-config",
        "status": "pending-audit",
        "note": "Per-item discount config blob; tenant-owned menu state carried in JSON.",
    },
    {
        "identifier": "Restaurant.socials / preset_tags / cuisine_types / branding_configuration",
        "location": "restaurants_app/models.py:93-126",
        "kind": "json-uuid",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-profile",
        "status": "pending-audit",
        "note": "Restaurant-scoped config blobs; Secretary-editable JSONFields, not FKs.",
    },
    {
        "identifier": "OrderItem.selected_modifiers",
        "location": "orders_app/models.py:164",
        "kind": "json-uuid",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "pending-audit",
        "note": "Diner selections keyed {group_id: [choice_id]} referencing "
                "MenuItem.options UUIDs with NO FK integrity — must be validated "
                "against the item's own restaurant at order build.",
    },
    # --- raw UUID arrays -------------------------------------------------------
    {
        "identifier": "MenuItem.extras_applicable",
        "location": "restaurants_app/models.py:362",
        "kind": "uuid-array",
        "owner": "menu",
        "follow_up_domain": "menu-config",
        "status": "remediated",
        "note": "Array of applicable extra-item UUIDs. Write-time integrity is now "
                "enforced by SerializerPutMenuItem via "
                "restaurants_app/controllers/menu_relationships.py (typed UUID-list "
                "field; canonical lowercase strings; unique; same-restaurant, "
                "non-deleted, is_extra, non-self), and the persisted corpus was "
                "repaired by migration 0055_sanitize_menu_item_extras. Cross-tenant "
                "denial is proven by the two-tenant tests in "
                "tests_menu_relationship_integrity.py; PR #233 "
                "(menu_publication.py) remains the runtime read/order defence.",
    },
    {
        "identifier": "MenuItem tag_ids (write payload) -> MenuItemTag",
        "location": "restaurants_app/models.py:405 (sync_tag_links); edit_information.py:69",
        "kind": "uuid-array",
        "owner": "menu",
        "follow_up_domain": "menu-config",
        "status": "pending-audit",
        "note": "tag_ids is a write-only payload key, not a model field; tenant scoping "
                "is enforced in SerializerPutMenuItem.validate(), NOT by sync_tag_links.",
    },
    # --- denormalised restaurant ids / snapshots -------------------------------
    {
        "identifier": "OrderItem.item_name_snapshot / modifiers_snapshot / allergen_tags_snapshot",
        "location": "orders_app/models.py:170-174",
        "kind": "denormalised-id",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "pending-audit",
        "note": "Menu data copied onto the order row at creation; correctness depends "
                "on the source item belonging to the order's restaurant.",
    },
    {
        "identifier": "restaurant_id injected into menu-create payloads",
        "location": "restaurants_app/endpoints/restaurant_setup.py:597",
        "kind": "denormalised-id",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-setup",
        "status": "pending-audit",
        "note": "Server-derived from the section, then written into the payload; safe "
                "only because it is re-derived, not taken from the client.",
    },
    {
        "identifier": "DinifyTransaction restaurant_id from request body",
        "location": "finance_app/endpoints/transactions.py:27",
        "kind": "denormalised-id",
        "owner": "finance",
        "follow_up_domain": "finance",
        "status": "pending-audit",
        "note": "restaurant_id read straight from the request body (not FK-resolved / "
                "ownership-gated) for transaction creation.",
    },
    # --- direct controller/service model writes (bypass serializers) -----------
    {
        "identifier": "Order.objects.create(...)",
        "location": "orders_app/controllers/services/create_order.py:160",
        "kind": "direct-write",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "pending-audit",
        "note": "The order row is created directly (SerializerPutOrder bypassed); "
                "table/restaurant tenant consistency is the controller's responsibility.",
    },
    {
        "identifier": "DinifyTransaction.objects.create(...)",
        "location": "finance_app/controllers/tx_subscription.py:48",
        "kind": "direct-write",
        "owner": "finance",
        "follow_up_domain": "finance",
        "status": "pending-audit",
        "note": "Subscription billing write, no serializer.",
    },
    {
        "identifier": "RestaurantRolePermission get_or_create / update_or_create",
        "location": "restaurants_app/controllers/role_permissions.py:55,161",
        "kind": "direct-write",
        "owner": "restaurants",
        "follow_up_domain": "roles-access",
        "status": "pending-audit",
        "note": "Role-grid writes keyed on (restaurant, role); owner-gated at the endpoint.",
    },
    {
        "identifier": "Table.objects.bulk_create / SectionGroup bulk_create",
        "location": "restaurants_app/controllers/tables.py:72; restaurants_app/endpoints/restaurant_setup.py:691",
        "kind": "direct-write",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-setup",
        "status": "pending-audit",
        "note": "Bulk row creation bypassing per-row serializer validation.",
    },
    # --- dynamic model dispatch ------------------------------------------------
    {
        "identifier": "Secretary self.serializer.Meta.model.objects.<op>",
        "location": "misc_app/controllers/secretary.py:104,277,343,505",
        "kind": "dynamic-dispatch",
        "owner": "platform",
        "follow_up_domain": "crud-engine",
        "status": "pending-audit",
        "note": "The central CRUD engine resolves the model from whatever serializer it "
                "is handed and applies NO tenant scoping — callers (e.g. restaurant_setup "
                "check_permission / scope_list_filter) must gate.",
    },
    {
        "identifier": "get_detail serializer.Meta.model.objects.get(id=...)",
        "location": "restaurants_app/endpoints/restaurant_setup.py:1232",
        "kind": "dynamic-dispatch",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-setup",
        "status": "pending-audit",
        "note": "Model chosen from a serializer map keyed by client-supplied record; the "
                "per-record module gate is the tenant boundary.",
    },
    # --- cache / idempotency keys ----------------------------------------------
    {
        "identifier": "Order.client_order_id (unique per (restaurant, client_order_id))",
        "location": "orders_app/models.py:61,122-126",
        "kind": "cache-idempotency-key",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "pending-audit",
        "note": "Client-supplied idempotency UUID; scoped to the restaurant by a partial "
                "unique constraint. Cross-tenant reuse must not collide or leak.",
    },
    {
        "identifier": "RestaurantDailyOrderCounter (restaurant, order_date)",
        "location": "orders_app/controllers/services/create_order.py:52-86; orders_app/models.py:127-129",
        "kind": "cache-idempotency-key",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "pending-audit",
        "note": "Per-restaurant daily order numbering under select_for_update; tenant key "
                "correctness gates order-number uniqueness.",
    },
]


def validate_inventory(inventory=INVENTORY):
    """
    Well-formedness violations for the inventory (used by the meta-test). Each entry
    must be a dict carrying exactly ``REQUIRED_KEYS``, a ``kind`` in ``KINDS`` and a
    ``status`` in ``STATUSES``, with non-empty string values. This checks SHAPE, not
    tenant-safety — the inventory is an audit list, not a proof.
    """
    problems = []
    for i, entry in enumerate(inventory):
        if not isinstance(entry, dict):
            problems.append(f"entry {i}: not a dict")
            continue
        keys = set(entry)
        if keys != REQUIRED_KEYS:
            problems.append(
                f"entry {i} ({entry.get('identifier', '?')}): keys {sorted(keys)} "
                f"!= required {sorted(REQUIRED_KEYS)}"
            )
            continue
        if entry["kind"] not in KINDS:
            problems.append(f"entry {i}: kind {entry['kind']!r} not in {sorted(KINDS)}")
        if entry["status"] not in STATUSES:
            problems.append(f"entry {i}: status {entry['status']!r} not in {sorted(STATUSES)}")
        for k in REQUIRED_KEYS:
            if not isinstance(entry[k], str) or not entry[k].strip():
                problems.append(f"entry {i}: field {k!r} must be a non-empty string")
    return problems
