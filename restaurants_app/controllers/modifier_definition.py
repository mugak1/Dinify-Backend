"""
Pure structural inspection of a stored ``MenuItem.options`` modifier definition.

``MenuItem.options`` is an unvalidated ``JSONField``: nothing on the write path
checks its shape, so the corpus can hold anything an operator tool ever wrote.
Checkout used to read it optimistically and several shapes reached a hashing or
comparison operation and raised — a malformed CATALOGUE row turning a diner's
order into a 500.

This module is the ONE place that decides what a stored definition means. It is
pure (no database, no Django model access, no settings) so the checkout
normaliser and the read-only preflight command can reach the same verdict instead
of maintaining a second policy in SQL.

THREE OUTCOMES, deliberately kept apart:

* **inactive** — ``hasModifiers`` falsy, or no groups. A legitimate, extremely
  common state: the item simply has no options. Orderable.
* **valid** — an active definition whose structure is unambiguous. Orderable.
* **invalid** — an active definition that is malformed or self-contradictory.
  FAIL CLOSED: checkout refuses the line. It is never silently downgraded to
  "no modifiers required", because that would delete a required selection to
  make a broken item orderable, which is the one outcome worse than refusing it.

CAPACITY IS NOT VALIDITY. A large catalogue is not a problem: an item may define
a hundred choices and remain perfectly orderable when the diner picks one. Only a
REQUIREMENT that cannot be satisfied within the request ceilings makes an item
unorderable, and that is reported separately as a compatibility concern —
never as a definition error and never as a new catalogue-size restriction.

IDENTIFIERS ARE OPAQUE. A group/choice id is whatever the operator's tool wrote;
it is not required to be a UUID. It must only be usable AS an identifier:
present, hashable, and unambiguous within its scope. Ambiguity is the real
hazard — ``normalize_selected_modifiers`` resolved duplicate ids first-wins while
``determine_effective_unit_price`` and ``construct_option_items`` resolve them
last-wins, so a duplicated id validated against one definition and priced against
another. Duplicates are therefore refused rather than silently resolved. The SAME
choice id appearing in DIFFERENT groups is not a duplicate and stays legal.

NOTHING HERE VALIDATES MONEY. ``additionalCost`` is untouched; D02 owns pricing.
"""

# --- verdict kinds -----------------------------------------------------------

KIND_INACTIVE = 'inactive'
KIND_VALID = 'valid'
KIND_INVALID = 'invalid'

# --- invalid-definition reason codes (stable; safe to log) -------------------

OPTIONS_NOT_A_MAPPING = 'options_not_a_mapping'
GROUPS_NOT_A_LIST = 'groups_not_a_list'
GROUP_NOT_A_MAPPING = 'group_not_a_mapping'
GROUP_ID_INVALID = 'group_id_invalid'
DUPLICATE_GROUP_ID = 'duplicate_group_id'
CHOICES_NOT_A_LIST = 'choices_not_a_list'
CHOICE_NOT_A_MAPPING = 'choice_not_a_mapping'
CHOICE_ID_INVALID = 'choice_id_invalid'
DUPLICATE_CHOICE_ID = 'duplicate_choice_id'
SELECTION_BOUND_INVALID = 'selection_bound_invalid'
MAX_BELOW_MIN = 'max_below_min'

# --- compatibility-concern codes (valid shape, but unorderable) --------------

MIN_EXCEEDS_DEFINED_CHOICES = 'min_exceeds_defined_choices'
MIN_EXCEEDS_CHOICE_CEILING = 'min_exceeds_choice_ceiling'
REQUIRED_GROUPS_EXCEED_CEILING = 'required_groups_exceed_ceiling'


class DefinitionVerdict:
    """What one stored definition is. Immutable in practice; plain object so the
    module stays dependency-free."""

    __slots__ = ('kind', 'reason', 'group_id', 'groups', 'concerns',
                 'group_count', 'required_group_count', 'max_choices_in_a_group')

    def __init__(self, kind, reason=None, group_id=None, groups=(), concerns=(),
                 group_count=0, required_group_count=0,
                 max_choices_in_a_group=0):
        self.kind = kind
        #: invalid-definition reason code, or None
        self.reason = reason
        #: the offending group id when one can be named safely, else None
        self.group_id = group_id
        #: tuple of GroupSpec, in catalogue order (empty unless kind == valid)
        self.groups = tuple(groups)
        #: tuple of (code, group_id_or_None) compatibility concerns
        self.concerns = tuple(concerns)
        self.group_count = group_count
        self.required_group_count = required_group_count
        self.max_choices_in_a_group = max_choices_in_a_group

    @property
    def is_invalid(self):
        return self.kind == KIND_INVALID

    @property
    def is_active(self):
        return self.kind == KIND_VALID


class GroupSpec:
    """One structurally sound modifier group.

    ``choices_by_id`` is unambiguous BY CONSTRUCTION — duplicate ids are refused
    before a spec is built — which is what lets validation, label construction
    and pricing share one resolution instead of the first-wins/last-wins split
    they used to have.
    """

    __slots__ = ('group_id', 'min_selections', 'max_selections', 'choice_ids',
                 'choices_by_id', 'raw')

    def __init__(self, group_id, min_selections, max_selections, choice_ids,
                 choices_by_id, raw):
        self.group_id = group_id
        self.min_selections = min_selections
        #: 0 means UNLIMITED — the established contract, preserved.
        self.max_selections = max_selections
        #: choice ids in catalogue-definition order
        self.choice_ids = tuple(choice_ids)
        #: id -> the raw choice mapping, for callers that need its other fields
        self.choices_by_id = dict(choices_by_id)
        #: the raw group mapping, for callers that need its other fields
        self.raw = raw


def _is_strict_int(value):
    """``bool`` is an ``int`` subclass; a selection bound of ``True`` is a
    malformed definition, not a bound of 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _selection_bound(raw):
    """Return ``(value, ok)``. Absent/``None`` means "unset", which the existing
    contract reads as 0 — and 0 for ``maxSelections`` means unlimited."""
    if raw is None:
        return 0, True
    if not _is_strict_int(raw) or raw < 0:
        return 0, False
    return raw, True


def usable_identifier(value):
    """An id is usable when it is present, hashable and not an empty string.
    Identity is preserved exactly — never trimmed, cased or parsed."""
    if value is None:
        return False
    if isinstance(value, str) and not value:
        return False
    try:
        hash(value)
    except TypeError:
        return False
    return True


def inspect_modifier_definition(options, max_choices_per_group=None,
                                max_groups_per_line=None):
    """
    Classify a stored ``MenuItem.options`` value.

    ``max_choices_per_group`` / ``max_groups_per_line`` are the REQUEST ceilings.
    They are used ONLY to report compatibility concerns about requirements that
    cannot be satisfied; they never make a definition invalid and never restrict
    how large a catalogue may be. Pass ``None`` to skip that reporting.
    """
    if not isinstance(options, dict):
        # A non-mapping (including the legacy JSON-string form) carries no
        # active modifiers. Reading it as inactive is the established
        # behaviour and is safe: there is no requirement to drop.
        return DefinitionVerdict(KIND_INACTIVE)

    if not options.get('hasModifiers'):
        return DefinitionVerdict(KIND_INACTIVE)

    raw_groups = options.get('groups')
    if raw_groups is None:
        # The flag is set but no group list was ever written. There are no
        # groups, so there is no requirement to lose by reading this as
        # inactive — unlike the non-list case below, where a value we cannot
        # read (a JSON-encoded string, say) may well encode real requirements.
        return DefinitionVerdict(KIND_INACTIVE)
    if not isinstance(raw_groups, list):
        # ACTIVE modifiers whose container is unreadable. Fail closed: we cannot
        # know which selections were required.
        return DefinitionVerdict(KIND_INVALID, reason=GROUPS_NOT_A_LIST)
    if not raw_groups:
        return DefinitionVerdict(KIND_INACTIVE)

    groups = []
    concerns = []
    seen_group_ids = set()
    required_group_count = 0
    max_choices_in_a_group = 0

    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            return DefinitionVerdict(KIND_INVALID, reason=GROUP_NOT_A_MAPPING)

        group_id = raw_group.get('id')
        if not usable_identifier(group_id):
            return DefinitionVerdict(KIND_INVALID, reason=GROUP_ID_INVALID)
        if group_id in seen_group_ids:
            # Two groups answering to one id: validation would resolve the first
            # and pricing the last. Refuse rather than pick one.
            return DefinitionVerdict(
                KIND_INVALID, reason=DUPLICATE_GROUP_ID, group_id=group_id,
            )
        seen_group_ids.add(group_id)

        raw_choices = raw_group.get('choices')
        if raw_choices is None:
            raw_choices = []
        if not isinstance(raw_choices, list):
            return DefinitionVerdict(
                KIND_INVALID, reason=CHOICES_NOT_A_LIST, group_id=group_id,
            )

        choice_ids = []
        choices_by_id = {}
        seen_choice_ids = set()
        for raw_choice in raw_choices:
            if not isinstance(raw_choice, dict):
                return DefinitionVerdict(
                    KIND_INVALID, reason=CHOICE_NOT_A_MAPPING, group_id=group_id,
                )
            choice_id = raw_choice.get('id')
            if not usable_identifier(choice_id):
                return DefinitionVerdict(
                    KIND_INVALID, reason=CHOICE_ID_INVALID, group_id=group_id,
                )
            if choice_id in seen_choice_ids:
                # Same divergence as duplicate groups, one level down.
                return DefinitionVerdict(
                    KIND_INVALID, reason=DUPLICATE_CHOICE_ID, group_id=group_id,
                )
            seen_choice_ids.add(choice_id)
            choice_ids.append(choice_id)
            choices_by_id[choice_id] = raw_choice

        minimum, min_ok = _selection_bound(raw_group.get('minSelections'))
        maximum, max_ok = _selection_bound(raw_group.get('maxSelections'))
        if not (min_ok and max_ok):
            return DefinitionVerdict(
                KIND_INVALID, reason=SELECTION_BOUND_INVALID, group_id=group_id,
            )
        if maximum and maximum < minimum:
            # Self-contradictory: no selection count satisfies both. Refuse
            # rather than invent a default, which would open a bypass.
            return DefinitionVerdict(
                KIND_INVALID, reason=MAX_BELOW_MIN, group_id=group_id,
            )

        if minimum:
            required_group_count += 1
            if minimum > len(choice_ids):
                concerns.append((MIN_EXCEEDS_DEFINED_CHOICES, group_id))
            if (max_choices_per_group is not None
                    and minimum > max_choices_per_group):
                concerns.append((MIN_EXCEEDS_CHOICE_CEILING, group_id))

        max_choices_in_a_group = max(max_choices_in_a_group, len(choice_ids))
        groups.append(GroupSpec(
            group_id, minimum, maximum, choice_ids, choices_by_id, raw_group,
        ))

    if (max_groups_per_line is not None
            and required_group_count > max_groups_per_line):
        # Only REQUIRED groups have to be submitted; a long optional list is
        # ordinary capacity, not an obstacle.
        concerns.append((REQUIRED_GROUPS_EXCEED_CEILING, None))

    return DefinitionVerdict(
        KIND_VALID,
        groups=groups,
        concerns=concerns,
        group_count=len(groups),
        required_group_count=required_group_count,
        max_choices_in_a_group=max_choices_in_a_group,
    )
