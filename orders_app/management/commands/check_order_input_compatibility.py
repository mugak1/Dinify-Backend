"""
D01 preflight — a READ-ONLY observation of whether a target database is ready
for the order-input contract and its ``quantity >= 0`` constraint.

It answers two independent questions and keeps their answers apart:

1. **Will the migration apply?** Is there any persisted ``OrderItem.quantity``
   below zero — across EVERY row, soft-deleted, archived and vacuumed included,
   because a ``CHECK`` constraint validates all of them and does not care about
   application-level flags.
2. **Will the catalogue still be orderable?** Do any stored modifier definitions
   fail the structural rules checkout now applies, or carry requirements that
   cannot be satisfied within the request ceilings.

CATALOGUE SIZE IS NOT A PROBLEM, and this command is careful never to imply it
is. An item may define a hundred choices and stay perfectly orderable when the
diner picks one; a long list of OPTIONAL groups is ordinary capacity. Only a
REQUIREMENT that cannot be met — a minimum above the number of defined choices,
a minimum above the per-group request ceiling, or more required groups than a
request may carry — actually blocks an order, and only those are reported as
concerns. Everything else is informational.

IT SHARES CHECKOUT'S RULES RATHER THAN RESTATING THEM IN SQL.
``inspect_modifier_definition`` is the same pure function
``ConOrder.normalize_selected_modifiers`` uses, so this command cannot drift
into a second opinion about what a catalogue row means. The definitions are
inspected in Python for exactly that reason — a JSON expression in SQL would be
a different policy, and an aggregate such as "the largest groups array" would
answer a question nobody asked and prove nothing about compatibility.

READ-ONLY, AND STRUCTURALLY SO. It has no fix, repair or ``--write`` mode. It
performs no model save, no bulk update, no audit-database write, no
notification, no provider call and no external side effect of any kind.
Remediating a violation is a business decision with an owner; a preflight's job
is to show the owner what is there.

IT RUNS AGAINST THE PRE-MIGRATION SCHEMA. It reads only columns that exist
before ``orders_app/0036``, so it is usable to DECIDE whether to deploy that
migration rather than only to confirm it afterwards.

IT REPORTS COUNTS AND BOUNDED SAMPLES — primary keys and stable reason codes,
never catalogue JSON, order contents, customer data or credentials. Rows are
streamed in chunks, so memory does not grow with the size of the catalogue.

AN INCOMPLETE INSPECTION IS NEVER REPORTED AS A CLEAN ONE: any failure during
the scan exits non-zero and says so.

    python manage.py check_order_input_compatibility
    python manage.py check_order_input_compatibility --sample-size 50 --chunk-size 1000

Exit codes:

    0  clean — no blockers, no compatibility concerns
    1  BLOCKER — negative persisted quantities exist; the migration will abort
    2  compatibility concerns only — the migration will apply, but some items
       are not orderable as configured
    3  the inspection did not complete; the result is unknown

A PREFLIGHT IS A POINT-IN-TIME OBSERVATION. It is not a substitute for the
database constraint, which is what keeps the invariant true afterwards.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from orders_app.controllers.services.order_input import (
    MAX_CHOICES_PER_GROUP,
    MAX_EXTRAS_PER_LINE,
    MAX_MODIFIER_GROUPS_PER_LINE,
    MAX_SELECTION_ENTRIES_PER_REQUEST,
    is_submittable_identifier,
)
from orders_app.models import OrderItem
from restaurants_app.controllers.menu_publication import (
    item_structurally_published,
)
from restaurants_app.controllers.modifier_definition import (
    MIN_EXCEEDS_CHOICE_CEILING,
    MIN_EXCEEDS_DEFINED_CHOICES,
    REQUIRED_CHOICES_NOT_SUBMITTABLE,
    REQUIRED_GROUP_ID_NOT_SUBMITTABLE,
    REQUIRED_GROUPS_EXCEED_CEILING,
    inspect_modifier_definition,
)
from misc_app.controllers.money import parse_money
from orders_app.controllers.services.order_pricing import (
    PricingRefused, modifier_adjustment,
)
from restaurants_app.controllers.pricing_policy import (
    DISCOUNT_EXCEEDS_PRICE, DISCOUNT_UNREADABLE, PRICE_UNREADABLE, resolve_price,
)
from restaurants_app.controllers.menu_publication import (
    normalize_extras_applicable,
)
from restaurants_app.models import MenuItem

#: Bound on the unpriceable-id set the extras axis needs. Past it the extras
#: check reports itself NOT DONE rather than growing without limit — an
#: incomplete inspection must never read as a clean one.
MAX_TRACKED_UNPRICEABLE = 10_000

EXIT_CLEAN = 0
EXIT_BLOCKER = 1
EXIT_CONCERNS = 2
EXIT_INCOMPLETE = 3

#: An extras minimum above this cannot be submitted, so the item is unorderable.
EXTRAS_MIN_EXCEEDS_CEILING = 'extras_min_exceeds_ceiling'

DEFAULT_SAMPLE_SIZE = 20
MAX_SAMPLE_SIZE = 200
DEFAULT_CHUNK_SIZE = 500


def _short(value, limit=24):
    """A bounded, printable form of an opaque identifier. Never the catalogue
    JSON, never an unbounded operator string."""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + '…'


def _as_uuid(value):
    """Parse a stored allowlist member to a UUID, or ``None``. The allowlist is
    canonical lowercase-UUID strings; the ids collected during the scan are real
    UUID objects, so one side has to be converted to compare them."""
    import uuid as _uuid
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


class _Bucket:
    """A bounded tally: a full count plus at most ``limit`` sample ids."""

    def __init__(self, limit):
        self.count = 0
        self.limit = limit
        self.samples = []

    def add(self, identifier, detail=None):
        self.count += 1
        if len(self.samples) < self.limit:
            self.samples.append(
                f'{identifier}' if detail is None else f'{identifier} ({detail})'
            )

    def __bool__(self):
        return self.count > 0


class Command(BaseCommand):
    help = (
        'Read-only D01 preflight: report persisted negative order-item '
        'quantities and stored modifier definitions that the order-input '
        'contract would refuse. Changes nothing.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--sample-size', type=int, default=DEFAULT_SAMPLE_SIZE,
            help=(
                'How many example ids to print per finding '
                f'(default {DEFAULT_SAMPLE_SIZE}, maximum {MAX_SAMPLE_SIZE}).'
            ),
        )
        parser.add_argument(
            '--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE,
            help=(
                'Rows fetched per batch while streaming the catalogue '
                f'(default {DEFAULT_CHUNK_SIZE}).'
            ),
        )

    # -- output helpers ---------------------------------------------------
    def _heading(self, text):
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(text))

    def _report(self, label, bucket, style=None):
        line = f'  {label}: {bucket.count}'
        self.stdout.write(style(line) if style else line)
        if bucket.samples:
            shown = ', '.join(bucket.samples)
            more = '' if bucket.count <= len(bucket.samples) else ', ...'
            self.stdout.write(f'      e.g. {shown}{more}')

    # -- the inspection ---------------------------------------------------
    def handle(self, *args, **options):
        sample_size = options['sample_size']
        chunk_size = options['chunk_size']
        if not 1 <= sample_size <= MAX_SAMPLE_SIZE:
            raise CommandError(
                f'--sample-size must be between 1 and {MAX_SAMPLE_SIZE}.'
            )
        if chunk_size < 1:
            raise CommandError('--chunk-size must be at least 1.')

        self.stdout.write(
            'D01 order-input preflight (read-only; nothing is modified).'
        )

        try:
            blockers = self._inspect_quantities(sample_size)
            concerns, informational, scanned = self._inspect_catalogue(
                sample_size, chunk_size,
            )
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            # An incomplete inspection must never read as a clean one.
            self.stderr.write(self.style.ERROR(
                f'INSPECTION DID NOT COMPLETE: {type(error).__name__}: {error}'
            ))
            self.stderr.write(self.style.ERROR(
                'The result is UNKNOWN. Do not treat this as a pass.'
            ))
            return self._exit(EXIT_INCOMPLETE)

        self._heading(
            '1. DATABASE CONSTRAINT BLOCKERS (orders_app/0036)'
        )
        self._report(
            'order items with a negative quantity', blockers,
            self.style.ERROR if blockers else None,
        )
        if blockers:
            self.stdout.write(
                '      The migration will ABORT on these rows and change '
                'nothing. They are real historical orders: decide on '
                'remediation explicitly — never clamp or delete them here.'
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                '      No blockers. The constraint can be added.'
            ))

        self._heading(
            f'2. CATALOGUE COMPATIBILITY  ({scanned} menu items inspected)'
        )
        any_concern = False
        for label, bucket in concerns:
            if bucket:
                any_concern = True
            self._report(label, bucket, self.style.WARNING if bucket else None)
        if not any_concern:
            self.stdout.write(self.style.SUCCESS(
                '      No concerns. Every inspected definition is orderable '
                'within the request ceilings.'
            ))

        self._heading('3. INFORMATIONAL (capacity only — NOT problems)')
        for label, bucket in informational:
            self._report(label, bucket)
        self.stdout.write(
            '      A large optional catalogue is ordinary capacity: an item '
            'may define many choices and stay orderable when the diner picks '
            'one. Nothing here blocks anything.'
        )

        self._heading('RESULT')
        if blockers:
            self.stdout.write(self.style.ERROR(
                'BLOCKED — resolve the negative quantities before deploying '
                'orders_app/0036.'
            ))
            return self._exit(EXIT_BLOCKER)
        if any_concern:
            self.stdout.write(self.style.WARNING(
                'MIGRATION SAFE, BUT REVIEW — the constraint can be added; '
                'some items are not orderable as configured.'
            ))
            return self._exit(EXIT_CONCERNS)
        self.stdout.write(self.style.SUCCESS(
            'CLEAN at this moment. This is a point-in-time observation, not a '
            'substitute for the constraint.'
        ))
        return self._exit(EXIT_CLEAN)

    #: Why each non-zero exit happened, so the CLI's last line is meaningful
    #: rather than a bare number.
    EXIT_REASONS = {
        EXIT_BLOCKER: (
            'negative order-item quantities exist; orders_app/0036 will abort'
        ),
        EXIT_CONCERNS: (
            'no migration blockers, but some catalogue items are not orderable '
            'as configured'
        ),
        EXIT_INCOMPLETE: 'the inspection did not complete; result unknown',
    }

    def _exit(self, code):
        """A non-zero exit is what makes this usable as an operator gate. It is
        raised rather than returned because Django prints a command's return
        value and exits 0 regardless."""
        self.exit_code = code
        if code:
            raise CommandError(self.EXIT_REASONS[code], returncode=code)
        return None

    # -- section 1 --------------------------------------------------------
    def _inspect_quantities(self, sample_size):
        """Every row, including soft-deleted / archived / vacuumed ones — the
        constraint covers all of them, so the preflight must too."""
        bucket = _Bucket(sample_size)
        offending = (
            OrderItem.objects
            .filter(quantity__lt=0)
            .order_by('pk')
            .values_list('pk', 'quantity')
        )
        bucket.count = offending.count()
        for pk, quantity in offending[:sample_size]:
            if len(bucket.samples) < sample_size:
                bucket.samples.append(f'{pk} (quantity={quantity})')
        return bucket

    # -- section 2 --------------------------------------------------------
    # ------------------------------------------------------------------
    # D02 MONETARY COMPATIBILITY
    #
    # The structural pass above answers "is this definition well-formed?". It
    # states in terms that it "validates NO monetary configuration", so it
    # CANNOT certify a catalogue against D02's new price refusals: an item whose
    # `primary_price` is unreadable, or whose currently-scheduled discount is
    # incoherent, is now neither published nor orderable, and nothing here used
    # to say so.
    #
    # IT REUSES THE SHARED PRIMITIVES — `resolve_price` and `parse_money`, the
    # exact functions checkout calls — so the preflight and the runtime cannot
    # hold different opinions. The structural inspector is NOT taught about
    # money; the two passes stay separate questions over one streamed scan.
    # ------------------------------------------------------------------

    #: The reasons `resolve_price` can refuse, rendered for an operator.
    PRICE_REASONS = {
        PRICE_UNREADABLE: 'the stored price cannot be read as money',
        DISCOUNT_UNREADABLE: 'a LIVE discount magnitude cannot be read',
        DISCOUNT_EXCEEDS_PRICE:
            'a LIVE discount is incoherent (below zero, or above the price)',
    }

    def _inspect_money(self, item, verdict, live, now, buckets):
        """One item's monetary configuration. Pure inspection; no query."""
        state = 'orderable' if live else 'draft/inactive'

        price = resolve_price(item.primary_price, item.discount_details, now=now)
        if not price.usable:
            reason = self.PRICE_REASONS.get(price.reason, price.reason)
            target = (buckets['unpriceable_live'] if live
                      else buckets['unpriceable_inactive'])
            target.add(item.pk, reason)
            # An unpriceable item is not orderable at all, so its modifier
            # adjustments cannot be reached. Saying so once is enough.
            return price

        if not verdict.is_active:
            return price

        # --- modifier adjustments ---------------------------------------
        #
        # THREE OUTCOMES, DELIBERATELY APART. A required group whose choices are
        # ALL unreadable makes EVERY variant of the dish refuse; one unreadable
        # choice beside readable ones does not, and reporting the two the same
        # way would tell an operator to take a perfectly orderable dish down.
        negative_total = parse_money(0)
        any_unreadable = False
        for group in verdict.groups:
            readable = 0
            unreadable = 0
            for choice_id in group.choice_ids:
                raw = (group.choices_by_id.get(choice_id) or {})
                try:
                    # THE CHECKOUT PRIMITIVE, CALLED THE WAY CHECKOUT CALLS IT.
                    # `con_orders` reads `choice.get('additionalCost', 0)` with
                    # no `or 0`, so a stored None / '' / [] / {} / False is a
                    # REFUSAL there, not a free option. An `or 0` here would have
                    # been quietly more permissive than the thing this command
                    # exists to predict, and would have reported a catalogue
                    # clean that checkout then refuses — the exact drift passing
                    # the shared primitive in is meant to prevent.
                    adjustment = modifier_adjustment(
                        raw.get('additionalCost', 0))
                except PricingRefused:
                    unreadable += 1
                    continue
                readable += 1
                if adjustment < 0:
                    negative_total += adjustment
            if unreadable:
                any_unreadable = True
                if group.min_selections > 0 and readable == 0:
                    buckets['required_group_unpriceable'].add(
                        item.pk,
                        f'group {_short(group.group_id)}: every choice '
                        f'unreadable, {state}',
                    )
        if any_unreadable:
            buckets['some_choice_unpriceable'].add(item.pk, state)

        # --- signed adjustments: the bound, and what it does NOT prove ----
        #
        # A negative adjustment ("no cheese, -500") is legal, and checkout
        # refuses only a line whose PAYABLE UNIT would go below zero. Which
        # combinations do that is a question about the diner's selection, and
        # enumerating a catalogue's variants would be combinatorial and would
        # still not be a proof. So the WORST CASE is bounded instead: every
        # negative adjustment taken at once. If even that stays at or above
        # zero, no selection can be refused for this reason. If it does not, SOME
        # selection can be — and this bound IGNORES per-group maxima, so it
        # over-reports rather than missing a case.
        if negative_total < 0:
            worst = price.effective_base + negative_total
            if worst < 0:
                buckets['negative_combination_possible'].add(
                    item.pk,
                    f'worst-case unit {worst} (ignores group maxima), {state}',
                )
        return price

    def _inspect_catalogue(self, sample_size, chunk_size):
        invalid = _Bucket(sample_size)
        invalid_inactive = _Bucket(sample_size)
        min_over_choices = _Bucket(sample_size)
        min_over_ceiling = _Bucket(sample_size)
        required_groups_over = _Bucket(sample_size)
        unnameable_group = _Bucket(sample_size)
        unnameable_choices = _Bucket(sample_size)
        extras_min_over = _Bucket(sample_size)
        combined_over = _Bucket(sample_size)
        wide_groups = _Bucket(sample_size)
        wide_choices = _Bucket(sample_size)
        money = {
            'unpriceable_live': _Bucket(sample_size),
            'unpriceable_inactive': _Bucket(sample_size),
            'required_group_unpriceable': _Bucket(sample_size),
            'some_choice_unpriceable': _Bucket(sample_size),
            'negative_combination_possible': _Bucket(sample_size),
            'required_extras_unpriceable': _Bucket(sample_size),
        }

        # ONE OBSERVATION TIME for the whole scan. A discount window is
        # time-dependent, so reading the clock per item would let a window close
        # mid-scan and report two items under two different presents — a report
        # describing no single moment.
        now = timezone.localtime()

        # Items whose price the server cannot read, collected as the scan goes so
        # the extras axis needs no per-parent query. BOUNDED: past the cap the
        # extras check reports itself incomplete rather than growing without
        # limit or quietly checking less than it claims.
        unpriceable_ids = set()
        unpriceable_overflowed = False

        scanned = 0
        rows = (
            MenuItem.objects
            .only(
                'id', 'options', 'approved', 'enabled', 'deleted',
                'has_extras', 'extras_min_selections', 'extras_applicable',
                'primary_price', 'discount_details',
            )
            .order_by('pk')
            .iterator(chunk_size=chunk_size)
        )
        for item in rows:
            scanned += 1
            live = item_structurally_published(item)
            state = 'orderable' if live else 'draft/inactive'

            verdict = inspect_modifier_definition(
                item.options,
                max_choices_per_group=MAX_CHOICES_PER_GROUP,
                max_groups_per_line=MAX_MODIFIER_GROUPS_PER_LINE,
                # THE request contract itself, not a restatement of it.
                identifier_predicate=is_submittable_identifier,
            )

            price = self._inspect_money(item, verdict, live, now, money)
            if not price.usable:
                if len(unpriceable_ids) < MAX_TRACKED_UNPRICEABLE:
                    unpriceable_ids.add(item.pk)
                else:
                    unpriceable_overflowed = True

            if verdict.is_invalid:
                target = invalid if live else invalid_inactive
                target.add(item.pk, f'{verdict.reason}, {state}')
            elif verdict.is_active:
                for code, group_id in verdict.concerns:
                    detail = f'{state}'
                    if code == MIN_EXCEEDS_DEFINED_CHOICES:
                        min_over_choices.add(item.pk, detail)
                    elif code == MIN_EXCEEDS_CHOICE_CEILING:
                        min_over_ceiling.add(item.pk, detail)
                    elif code == REQUIRED_GROUPS_EXCEED_CEILING:
                        required_groups_over.add(item.pk, detail)
                    elif code == REQUIRED_GROUP_ID_NOT_SUBMITTABLE:
                        unnameable_group.add(item.pk, detail)
                    elif code == REQUIRED_CHOICES_NOT_SUBMITTABLE:
                        unnameable_choices.add(item.pk, detail)
                if verdict.group_count > MAX_MODIFIER_GROUPS_PER_LINE:
                    wide_groups.add(item.pk, f'{verdict.group_count} groups')
                if verdict.max_choices_in_a_group > MAX_CHOICES_PER_GROUP:
                    wide_choices.add(
                        item.pk,
                        f'{verdict.max_choices_in_a_group} choices in a group',
                    )

            # Extras are a separate axis from modifier groups.
            minimum = item.extras_min_selections or 0
            required_extras = minimum if item.has_extras else 0
            if item.has_extras and minimum > MAX_EXTRAS_PER_LINE:
                extras_min_over.add(item.pk, state)

            # THE TWO AXES SHARE ONE WHOLE-REQUEST CEILING, so checking them
            # separately is not enough: 32 groups each requiring 64 choices is
            # exactly at the limit, and a single required extra beside it puts
            # the only satisfying request one entry over. Compare the COMBINED
            # minimum — the fewest entries any satisfying request could carry.
            combined_required = verdict.required_selection_entries + required_extras
            if combined_required > MAX_SELECTION_ENTRIES_PER_REQUEST:
                combined_over.add(
                    item.pk, f'{combined_required} required entries, {state}',
                )

        # --- the extras axis, resolved WITHOUT a per-parent query -------
        #
        # An extra whose price the server cannot read is not published, so a
        # parent that REQUIRES extras can end up with too few to satisfy its own
        # minimum — a dish that is not orderable for a reason neither the
        # structural pass nor the parent's own price can see. It needs the set of
        # unpriceable ids, which only exists once the first pass has finished, so
        # it is a second STREAMING pass over the parents that actually require
        # extras rather than a lookup per item.
        if unpriceable_ids and not unpriceable_overflowed:
            parents = (
                MenuItem.objects
                .filter(has_extras=True, extras_min_selections__gt=0)
                .only(
                    'id', 'extras_applicable', 'extras_min_selections',
                    'approved', 'enabled', 'deleted',
                )
                .order_by('pk')
                .iterator(chunk_size=chunk_size)
            )
            for parent in parents:
                allowed = normalize_extras_applicable(parent.extras_applicable)
                if not allowed:
                    continue
                usable = sum(
                    1 for extra_id in allowed
                    if _as_uuid(extra_id) not in unpriceable_ids
                )
                minimum = parent.extras_min_selections or 0
                if usable < minimum:
                    state = ('orderable'
                             if item_structurally_published(parent)
                             else 'draft/inactive')
                    money['required_extras_unpriceable'].add(
                        parent.pk,
                        f'{usable} priceable of {len(allowed)} configured, '
                        f'{minimum} required, {state}',
                    )

        concerns = [
            ('active definitions checkout will now REFUSE', invalid),
            ('required selections above the defined choices', min_over_choices),
            (
                'required selections above the per-group request ceiling '
                f'({MAX_CHOICES_PER_GROUP})',
                min_over_ceiling,
            ),
            (
                'required groups above the per-line request ceiling '
                f'({MAX_MODIFIER_GROUPS_PER_LINE})',
                required_groups_over,
            ),
            (
                'required extras above the per-line request ceiling '
                f'({MAX_EXTRAS_PER_LINE})',
                extras_min_over,
            ),
            (
                'required option groups no request can name',
                unnameable_group,
            ),
            (
                'required groups whose choices no request can name',
                unnameable_choices,
            ),
            (
                'combined required options + extras above the whole-request '
                f'ceiling ({MAX_SELECTION_ENTRIES_PER_REQUEST})',
                combined_over,
            ),
            # --- D02 MONETARY -------------------------------------------
            (
                'LIVE items the server cannot price (not published, not '
                'orderable)',
                money['unpriceable_live'],
            ),
            (
                'required option groups whose every choice has an unreadable '
                'cost (no variant of the dish is orderable)',
                money['required_group_unpriceable'],
            ),
            (
                'dishes whose required extras cannot all be priced (too few '
                'publishable to meet the minimum)',
                money['required_extras_unpriceable'],
            ),
        ]
        informational = [
            (
                'malformed definitions on DRAFT/INACTIVE items '
                '(not orderable anyway)',
                invalid_inactive,
            ),
            (
                f'items defining more than {MAX_MODIFIER_GROUPS_PER_LINE} '
                'option groups',
                wide_groups,
            ),
            (
                f'items with more than {MAX_CHOICES_PER_GROUP} choices in one '
                'group',
                wide_choices,
            ),
            # --- D02 MONETARY, and NOT concerns -------------------------
            (
                'items the server cannot price on DRAFT/INACTIVE items '
                '(not orderable anyway)',
                money['unpriceable_inactive'],
            ),
            (
                'items with SOME unreadable choice cost — the dish still has '
                'orderable variants; picking that choice is what refuses',
                money['some_choice_unpriceable'],
            ),
            (
                'items where a combination of NEGATIVE adjustments could take '
                'the payable unit below zero — a worst-case bound that ignores '
                'group maxima, so which selections refuse is decidable only at '
                'checkout',
                money['negative_combination_possible'],
            ),
        ]
        if unpriceable_overflowed:
            self.stderr.write(self.style.WARNING(
                f'      NOTE: more than {MAX_TRACKED_UNPRICEABLE} unpriceable '
                'items were found, so the required-extras axis was NOT '
                'checked. Resolve the unpriceable items and re-run.'
            ))
        return concerns, informational, scanned
