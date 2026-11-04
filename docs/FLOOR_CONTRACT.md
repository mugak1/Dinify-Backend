# Floor contract v1 (Step 2): the backend's record

This is the shared shape for the backend floor endpoints (Step 2, PR BE-D) and the
frontend floor view. FE-B built the view against it on mock data; FE-C points it at
these endpoints. The text below is the contract as it was handed to BE-D, followed
by **what BE-D defined, clarified or changed while building it**. Nothing in that
second part changed silently: each item is also listed in the PR and in CLAUDE.md,
and FE-C adapts to it.

Code: `orders_app/controllers/services/round_release.py` (the commands and the round
projection), `orders_app/endpoints_floor.py` (the views), `orders_app/urls_floor.py`
(mounted at `api/v1/floor/`).

---

## The contract as handed over

* Every response uses the backend's usual `{status, message, data}` envelope.
* All times are ISO 8601 with an offset.
* Money is a decimal string from `misc_app/controllers/money.py`, never a float.

### Rules the shape encodes

* There is no payment field except `payment: "unknown"`. Step 2 records no payment,
  so nothing in this contract may claim one was made. In particular there is no
  `is_paid`, `current_check` or `balance`.
* `occupied` comes from orders matching `occupancy.ongoing_order_q`, never from
  `Table.status`. An order with no placement (one from before Step 1, or accepted
  with the writer switched off) appears as a released round with `visit: null`.
* `stale` is computed on the server: a held round is stale once
  `server_time - held_since >= 15 minutes`. It is a flag only; nothing acts on it.
* Tables that need staff come first, then the rest in table-number order. A table
  needs staff when it has at least one held round.

### GET `api/v1/floor/?restaurant=<uuid>`

Requires the `tables` module.

```json
{
  "contract": "floor.v1",
  "server_time": "2026-11-03T19:02:11+03:00",
  "restaurant": {
    "id": "uuid",
    "accepting_orders": true,
    "payment_timing": "pay_after"
  },
  "waiting_for_staff": 1,
  "tables": [
    {
      "id": "uuid",
      "number": 4,
      "display_name": "T4",
      "area_id": "uuid or null",
      "status": "available",
      "occupied": true,
      "needs_staff": true,
      "visit": { "id": "uuid", "opened_at": "…", "payment": "unknown" },
      "rounds": [
        {
          "order_id": "uuid",
          "round_number": 1,
          "state": "held",
          "source": "diner",
          "accepted_at": "…",
          "held_since": "…",
          "released_at": null,
          "stale": false,
          "fulfilment_status": "new",
          "item_count": 3,
          "value": "16000.00"
        }
      ]
    }
  ]
}
```

Field values:

* `restaurant.payment_timing` is `pay_after`, `pay_first`, `not_chosen` or `null`.
* `tables[].status` is the floor axis: `available`, `seated`, `bill_requested`,
  `dirty` or `out_of_service`.
* `rounds[].state` is `held` or `released`. A served, cancelled or declined round is
  not listed, because the floor shows only live rounds.
* `rounds[].source` is `diner` or `staff`.
* `rounds[].held_since` is set only for a held round.
* `rounds[].released_at` is set only for a released round.
* `rounds[].fulfilment_status` is the kitchen axis, meaningful only for a released
  round.
* `rounds[].value` is the round's value as a decimal string. A held round's value is
  "to pay", not owed.

### GET `api/v1/floor/tables/<uuid>/?restaurant=<uuid>`

The table drawer. It returns the same table object as above, and each round also
carries:

```json
{
  "items": [
    {
      "name": "Rolex",
      "quantity": 2,
      "extras": [{ "name": "Avocado", "quantity": 1 }],
      "modifiers": ["No onion"],
      "note": "string or null"
    }
  ],
  "cannot_be_made": { "reason": "item_sold_out", "item_name": "Rolex" }
}
```

* The item names are the snapshots taken at acceptance.
* `cannot_be_made` is `null` unless the round is held and fails the release
  makeability check.

### POST `api/v1/floor/rounds/<order_uuid>/confirm/` and `…/decline/`

* The body is `{"restaurant": "uuid"}`. Both require the `tables` module, and the
  delegated-session allow-list refuses both.
* 200: `{"round": <round object>, "changed": true|false}`. Resending the same
  command is safe: `changed: false` with the round as it now is.
* 409 for a crossed or blocked command, with `reason` set to one of:
  * `round_declined`, `round_released` or `round_not_held`;
  * `restaurant_not_trading`;
  * `ordering_paused`, `table_menu_only` or `table_out_of_service`, which apply to a
    diner round only;
  * `cannot_be_made`, which also carries `item_name`.

  The 409 also carries the round's current `state`.
* Decline takes no reason from the client. The server records
  `cancellation_reason = "declined_by_staff"`.

---

## What BE-D defined, clarified or changed

Numbered so a reviewer and FE-C can refer to them. **C** = a change to what the text
above says; **A** = additive (a key the text does not mention); **D** = a definition
the text left open.

### Changes

1. **C — `table_out_of_service` also applies to a STAFF round.** The text says the
   three operational reasons apply to a diner round only. Confirm asks
   `order_eligibility.evaluate`, exactly as acceptance does, and there table
   LIVENESS binds every provenance (D06): a staff round passes a pause
   (`ordering_paused`) and a menu-only table (`table_menu_only`), but a staff round
   at a table that is out of service, disabled, inactive or deleted is refused
   `table_out_of_service`. Only `ordering_paused` and `table_menu_only` are
   diner-only.
2. **C — `restaurant_not_trading` is not what a SUSPENDED restaurant answers.**
   Portal access is lifecycle-filtered: at a `suspended` or `offboarded` restaurant
   no member of staff (the owner included) holds the `tables` module, so both
   commands and both reads answer the one 404 before admission is consulted.
   `restaurant_not_trading` is what admission's refusal produces where access still
   holds: a DINER round at a restaurant that is still `onboarding` (the launch
   boundary). A suspension committing while a Confirm waits on its locks is
   likewise a 404 (the `tables` re-check under the lock).
3. **A — the round object carries `accepted_at_provenance`**: `acceptance` or
   `order_created`. See D-1.
4. **A — a command's round `state` can be outside `held` / `released`.** The floor
   lists live rounds only, but a command answers about the round it was sent for,
   which may have left the floor. A command's round `state` is one of `held`,
   `released`, `declined`, `cancelled` (cancelled by the kitchen), `served` or
   `draft`. A successful Decline answers `state: "declined"`.
5. **A — a 409 also carries `data.round`**, the round as it now is, mirroring the
   kitchen's `_conflict` envelope: `{status, message, reason, state, data: {round}}`.
   A `cannot_be_made` 409 also carries `cannot_be_made: {reason, item_name}`, the
   same object the drawer shows, beside the top-level `item_name`.

### Definitions

1. **D — `accepted_at` for an order with no `OrderAcceptance` row** (accepted before
   that record existed): the order's creation time (`time_created`), the moment the
   kitchen card already ages such a ticket from, labelled
   `accepted_at_provenance: "order_created"` so it is never read as a recorded
   acceptance. Every order accepted since has a row and reads `"acceptance"`; that
   includes an order with no placement accepted while the writer was off.
2. **D — `item_count`**: the total QUANTITY of the round's main dishes as the
   kitchen lists them: live (not deleted), top-level (no parent line) and
   `available` lines. Extras are not counted; a line the create path zeroed
   (`available=false`, quantity 0) is not counted. The same rule for every order,
   legacy or not: the lines carry no acceptance-dependent fact.
3. **D — `held_since`** is the round's acceptance instant (it has been held since it
   was accepted), so for a held round it equals `accepted_at`.
4. **D — `released_at` for a released round with NO placement is `null`**: its
   release was never recorded. The kitchen feeds say the same. For every other
   released round it is the placement's `released_at` (the acceptance instant for a
   round Step 1 released, the Confirm instant for a held round staff released).
5. **D — `value`** is `Order.actual_cost` (the payable), through `format_money`.
6. **D — `waiting_for_staff`** is the number of HELD ROUNDS on the floor (not tables).
7. **D — order**: needs-staff tables first, then the rest, each by table number (then
   `str_number`, then id). Rounds within a table by `accepted_at`.
8. **D — `visit`** is the visit of the table's first round that has one; `null` when
   none of its rounds has a placement. Until Step 3 a table normally holds one round.
9. **D — `display_name`** falls back exactly as the kitchen ticket label does:
   `display_name`, else `str_number`, else the number.
10. **D — `restaurant.payment_timing`** is the configured value (`pay_after` /
    `pay_first`) or `null` when nothing is configured. `not_chosen` is a visit's word
    and is not emitted at the restaurant level.
11. **D — `note`** is always `null`: no order line records a note.
12. **D — `modifiers`** is the line's `modifiers_snapshot` as saved, e.g.
    `["Onion: No onion"]` (group label, then the choices).
13. **D — `cannot_be_made.reason`** is the makeability classification:
    `item_sold_out`, `item_deleted`, `item_missing`, `section_or_group_deleted`,
    `extra_not_attachable`, `extras_bounds_no_longer_met`,
    `modifier_definition_removed`, `modifier_definition_invalid`,
    `modifier_group_missing`, `modifier_choice_missing`,
    `modifier_group_bounds_no_longer_met`, `modifier_selection_meaning_changed` or
    `allergen_declaration_changed`. `item_name` is the line's SAVED name. Publication
    and section schedules are not part of the check.
14. **D — the drawer's `data` is the table object itself** (no `contract` or
    `server_time` wrapper), as FE-B's mock already reads it.
15. **D — times** are rendered in the platform's zone (Africa/Nairobi, `+03:00`) to
    the millisecond, e.g. `2026-11-03T19:02:11.000+03:00`.
16. **D — status codes outside the 200/409 pair**: a missing or blank `restaurant` is
    `400`; a malformed id, an unknown one, one outside the named restaurant, and a
    caller without the `tables` module there are all the same
    `404 {"status": 404, "message": "Not found"}`; no credential is `401`; a
    delegated session is refused `403` by the middleware before dispatch.
17. **D — `tables[].status` is reported as stored.** It is the staff-set floor axis.

### Open, for the owner

* `Table.status` was writable through the generic restaurant-setup PUT before PR-5
  without validation, so a stored value outside the five may exist. The read passes
  the stored value through, and FE-B's adapter refuses a whole floor read on an
  unknown status. Whether to normalise is a data decision; it can be answered with
  one read-only query on UAT.
