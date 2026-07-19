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
its KIND, a domain OWNER, the follow-up DOMAIN, and a STATUS. Do not treat
``len(INVENTORY)`` as a vulnerability count; the accompanying meta-test only checks
the list is well-formed.

``status`` meaning (tightened by the TENANT-ISO-PR6A closure audit):
  * ``pending-audit`` — nobody has yet confirmed this path scopes by tenant.
  * ``audited``       — examined; the value is tenant-LOCAL data (or the boundary
                        sits at an already-tested endpoint), so it cannot SELECT
                        or BIND another tenant's row; no code change was needed.
  * ``remediated``    — a server-side invariant now enforces tenant scope AND an
                        adversarial test proves it; the note cites the exact
                        production chokepoint and the proving test.

The PR6A closure pass classified every remaining entry against these definitions
(see dinify_backend/tenancy/tests_tenant_isolation_closure.py and
dinify_backend/tenancy/TENANT_ISOLATION_CLOSURE.md). Entries are NOT flipped to
zero-out the list — each disposition below is backed by the evidence it cites.
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
        "status": "remediated",
        "note": "Grouped-modifier group/choice UUIDs live in a JSONField; no FK. A "
                "diner selection is validated ENTIRELY against the ordered item's OWN "
                "options at build time: ConOrder.determine_effective_unit_price "
                "(orders_app/controllers/con_orders.py) rejects any group/choice id "
                "not in menu_item.options and recomputes additional cost server-side, "
                "and TENANT-ISO-PR6A added the in-transaction min/max completeness "
                "re-check + duplicate-choice de-dup in _create_order / "
                "check_options_requirements. Because the parent item is itself "
                "tenant-scoped (validate_order_selections restaurant-scopes it), a "
                "modifier id can only ever reference the parent's own tenant-local "
                "options — never another tenant's. Proven by "
                "tests_tenant_isolation_closure.ModifierIntegrityClosureTests.",
    },
    {
        "identifier": "MenuItem.discount_details",
        "location": "restaurants_app/models.py:323",
        "kind": "json-uuid",
        "owner": "menu",
        "follow_up_domain": "menu-config",
        "status": "audited",
        "note": "Per-item discount config blob (percentages, dates, recurring_days) "
                "carried in a JSONField on a tenant-scoped MenuItem. It holds no "
                "cross-tenant selector: MenuItem.is_discount_active() / "
                "effective_base_price() read only numeric/date fields from the blob, "
                "and SerializerPutMenuItem validates its shape (an inverted date "
                "window is a 400). It is tenant-LOCAL config that cannot select or "
                "bind another tenant's row — no runtime tenant boundary rides on it.",
    },
    {
        "identifier": "Restaurant.socials / preset_tags / cuisine_types / branding_configuration",
        "location": "restaurants_app/models.py:93-126",
        "kind": "json-uuid",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-profile",
        "status": "audited",
        "note": "Restaurant-scoped display/config blobs (social URLs, branding colours, "
                "cuisine strings, preset tag labels) written ONLY through the "
                "settings-module-gated restaurant PUT (Secretary), so they can never "
                "be attached to another tenant's restaurant. They are presentational "
                "config, not relational selectors used in any cross-tenant query — "
                "tenant-LOCAL data that cannot select or bind another tenant.",
    },
    {
        "identifier": "OrderItem.selected_modifiers",
        "location": "orders_app/models.py:164",
        "kind": "json-uuid",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "remediated",
        "note": "Persisted diner selections keyed {group_id: [choice_id]}. They are "
                "written only after being validated against the ordered item's own "
                "tenant-scoped options (see MenuItem.options entry above) and the "
                "stored value / modifiers_snapshot derive solely from that validated "
                "parent in ConOrder.add_order_item — never from unvalidated client "
                "input. Proven by "
                "tests_tenant_isolation_closure.ModifierIntegrityClosureTests "
                "(valid/priced, foreign group/choice rejected, snapshot derivation).",
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
        "location": "restaurants_app/serializers.py:419-458 (SerializerPutMenuItem.validate); models.py:405 (sync_tag_links)",
        "kind": "uuid-array",
        "owner": "menu",
        "follow_up_domain": "menu-config",
        "status": "remediated",
        "note": "tag_ids is a typed write-only UUID-list field on the now-explicit "
                "SerializerPutMenuItem (no longer fields='__all__'). validate() "
                "resolves the restaurant from the item's section and rejects any id "
                "that is not a non-deleted RestaurantTag of THAT restaurant; "
                "sync_tag_links only ever runs on the validated set. Cross-tenant "
                "denial is proven by the two-tenant tests in "
                "restaurants_app.tests.MenuFkTenantBoundaryTests "
                "(test_foreign_tag_ids_still_rejected / test_same_tenant_tag_ids_still_succeeds).",
    },
    # --- denormalised restaurant ids / snapshots -------------------------------
    {
        "identifier": "OrderItem.item_name_snapshot / modifiers_snapshot / allergen_tags_snapshot",
        "location": "orders_app/models.py:170-174",
        "kind": "denormalised-id",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "audited",
        "note": "Menu data copied onto the order row at creation. The source is always "
                "the tenant-scoped parent fetched by "
                "ConOrder.add_order_item as "
                "MenuItem.objects.get(pk=item['item'], section__restaurant=order.restaurant) "
                "— a foreign/unknown id is a 400, never a snapshot. The snapshot is "
                "denormalised tenant-LOCAL data (names/tags/modifier strings), not a "
                "selector that can bind another tenant. Snapshot-from-validated-parent "
                "is exercised by "
                "tests_tenant_isolation_closure.ModifierIntegrityClosureTests."
                "test_modifier_snapshot_derives_from_validated_parent.",
    },
    {
        "identifier": "restaurant bound on menu-create via server_values",
        "location": "restaurants_app/endpoints/restaurant_setup.py:606-627",
        "kind": "denormalised-id",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-setup",
        "status": "remediated",
        "note": "The create path no longer injects a `restaurant` key into the "
                "request-shaped payload. MenuSection.restaurant is read_only on "
                "SerializerPutMenuSection and is bound server-side through the "
                "Secretary server_values channel (restaurant_id) from the "
                "already-gated resource; for sectiongroups/menuitems the restaurant "
                "is re-derived from the (SameTenant-classified) section, never taken "
                "from the client. A client `restaurant` key can no longer widen "
                "scope. Create-gate denial is proven by "
                "MenuFkTenantBoundaryTests.test_create_menuitem_with_foreign_section_denied "
                "and test_create_sectiongroup_with_foreign_section_denied.",
    },
    {
        "identifier": "DinifyTransaction restaurant_id from request body",
        "location": "finance_app/endpoints/transactions.py:27",
        "kind": "denormalised-id",
        "owner": "finance",
        "follow_up_domain": "finance",
        "status": "remediated",
        "note": "The body restaurant_id is authorized at the HTTP edge BEFORE use: "
                "TransactionsEndpoint calls can_manage_restaurant(request.user, "
                "restaurant_id) and returns a non-disclosing 404 on failure (fails "
                "closed on a null/empty id). A caller can therefore only ever bill a "
                "restaurant they manage. Proven by "
                "tests_tenant_isolation_closure.SubscriptionTransactionClosureTests "
                "(non-manager 404, cross-tenant 404, own-restaurant success).",
    },
    # --- direct controller/service model writes (bypass serializers) -----------
    {
        "identifier": "Order.objects.create(...)",
        "location": "orders_app/controllers/services/create_order.py:160",
        "kind": "direct-write",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "remediated",
        "note": "The row is created inside _create_order's transaction only after "
                "(a) the table is fetched restaurant-scoped in ConOrder.initiate_order "
                "(foreign/malformed table id -> 400), and (b) the load-bearing "
                "validate_order_selections re-check runs against committed menu state "
                "after the table lock — tenant ownership + publication + extra "
                "applicability + (PR6A) modifier limits, raising OrderItemRejected so "
                "the whole transaction unwinds (no order/item/counter). A direct "
                "service call cannot bypass this. Proven by "
                "tests_tenant_isolation_closure.MenuPublicationCheckoutClosureTests "
                "and ModifierIntegrityClosureTests (direct _create_order calls).",
    },
    {
        "identifier": "DinifyTransaction.objects.create(...)",
        "location": "finance_app/controllers/tx_subscription.py:48",
        "kind": "direct-write",
        "owner": "finance",
        "follow_up_domain": "finance",
        "status": "audited",
        "note": "Subscription billing write with no serializer. The tenant boundary "
                "sits at the ONLY caller, TransactionsEndpoint, which authorizes the "
                "restaurant via can_manage_restaurant before invoking the service "
                "(see the 'DinifyTransaction restaurant_id from request body' entry). "
                "The service is a trusted INTERNAL chokepoint: it must be handed an "
                "already-authorized restaurant_id, resolves the Restaurant, and "
                "derives transaction_amount = restaurant.flat_fee server-side (never "
                "from the request body). Amount-server-derivation + endpoint gating "
                "are proven by "
                "tests_tenant_isolation_closure.SubscriptionTransactionClosureTests.",
    },
    {
        "identifier": "RestaurantRolePermission get_or_create / update_or_create",
        "location": "restaurants_app/controllers/role_permissions.py:55,161",
        "kind": "direct-write",
        "owner": "restaurants",
        "follow_up_domain": "roles-access",
        "status": "remediated",
        "note": "Role-grid writes keyed on (restaurant, role) run only behind the "
                "owner-only MODULE_TEAM gate on RolePermissionsEndpoint, with explicit "
                "(non-Secretary) validation: owner row immutable, unknown role "
                "rejected, modules a dict of GRID_MODULES->strict bool, merged under "
                "select_for_update on the gated restaurant. Cross-tenant / validation "
                "denial is proven by restaurants_app.tests_role_permissions.",
    },
    {
        "identifier": "Table.objects.bulk_create / SectionGroup bulk_create",
        "location": "restaurants_app/controllers/tables.py:72; restaurants_app/endpoints/restaurant_setup.py:691",
        "kind": "direct-write",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-setup",
        "status": "remediated",
        "note": "Bulk rows are bound SERVER-SIDE to the already-gated restaurant, never "
                "per-row client input: create_dining_area/create_tables_in_section "
                "build every Table(restaurant=restaurant, dining_area=area) from the "
                "single resolved Restaurant (the endpoint check_permission gates the "
                "target restaurant first), and section-group bulk_create binds every "
                "SectionGroup to the just-created, authorized section. A partial "
                "failure is atomic (create_dining_area wraps transaction.atomic). "
                "Proven by "
                "tests_tenant_isolation_closure.BulkCreationTenantClosureTests "
                "(tables bound to restaurant, cross-tenant bulk 403, groups bound to "
                "the created section).",
    },
    # --- dynamic model dispatch ------------------------------------------------
    {
        "identifier": "Secretary self.serializer.Meta.model.objects.<op>",
        "location": "misc_app/controllers/secretary.py:299 (read() list path); update()/delete() now scope-bound",
        "kind": "dynamic-dispatch",
        "owner": "platform",
        "follow_up_domain": "crud-engine",
        "status": "remediated",
        "note": "update()/delete() resolve the row ONLY through a caller-supplied, "
                "server-built instance_queryset under select_for_update — the "
                "unrestricted Model.objects.get fallback was REMOVED and a missing "
                "scope fails closed (500), a foreign/unknown id is a non-enumerating "
                "404. create() writes created_by and any parent FK through the trusted "
                "server_values channel (all read_only on the migrated write "
                "serializers), never from request data. The only remaining dynamic "
                ".objects use is read() (a LIST path the endpoint scopes via "
                "scope_list_filter). Proven by misc_app.tests.SecretaryScopeBoundTests.",
    },
    {
        "identifier": "get_detail serializer.Meta.model.objects.get(id=...)",
        "location": "restaurants_app/endpoints/restaurant_setup.py:1266-1321",
        "kind": "dynamic-dispatch",
        "owner": "restaurants",
        "follow_up_domain": "restaurant-setup",
        "status": "remediated",
        "note": "The model/serializer is chosen from a fixed ALLOWLIST keyed on the "
                "client `record`; before the fetch, get_detail resolves the record's "
                "owning restaurant SERVER-SIDE via _RESTAURANT_RESOLVERS (walking the "
                "FK chain from the id) and gates the read on _RECORD_MODULE with "
                "can_user_access_module. A nonexistent id or unknown record type "
                "resolves to None -> 404 BEFORE the fetch (never passed as None), and "
                "cross-tenant access returns the SAME non-enumerating 404 — no client "
                "restaurant field can widen it. Proven by "
                "tests_tenant_isolation_closure.GetDetailDispatchClosureTests "
                "(own 200, cross-tenant/unknown indistinguishable 404, unmapped 404).",
    },
    # --- cache / idempotency keys ----------------------------------------------
    {
        "identifier": "Order.client_order_id (unique per (restaurant, client_order_id))",
        "location": "orders_app/models.py:61,122-126",
        "kind": "cache-idempotency-key",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "remediated",
        "note": "Client-supplied idempotency UUID scoped to the restaurant by the "
                "partial unique constraint uniq_order_restaurant_client_order_id; the "
                "_create_order replay lookup and the double-tap IntegrityError "
                "recovery both filter on (restaurant, client_order_id). The SAME uuid "
                "reused across two tenants yields two independent orders, and a replay "
                "returns only the same-tenant order — never another tenant's. Proven "
                "by tests_tenant_isolation_closure.IdempotencyCounterScopingClosureTests.",
    },
    {
        "identifier": "RestaurantDailyOrderCounter (restaurant, order_date)",
        "location": "orders_app/controllers/services/create_order.py:52-86; orders_app/models.py:127-129",
        "kind": "cache-idempotency-key",
        "owner": "orders",
        "follow_up_domain": "ordering",
        "status": "remediated",
        "note": "Per-restaurant daily numbering: allocate_daily_order_number does "
                "get_or_create(restaurant=..., order_date=...) under select_for_update "
                "with a (restaurant, order_date) unique constraint, so two tenants "
                "ordering the same day allocate from independent counter rows. Proven "
                "by tests_tenant_isolation_closure.IdempotencyCounterScopingClosureTests."
                "test_daily_counters_are_per_restaurant.",
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
