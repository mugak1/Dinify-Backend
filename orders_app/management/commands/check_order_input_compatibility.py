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

from orders_app.controllers.services.order_input import (
    MAX_CHOICES_PER_GROUP,
    MAX_EXTRAS_PER_LINE,
    MAX_MODIFIER_GROUPS_PER_LINE,
)
from orders_app.models import OrderItem
from restaurants_app.controllers.menu_publication import (
    item_structurally_published,
)
from restaurants_app.controllers.modifier_definition import (
    MIN_EXCEEDS_CHOICE_CEILING,
    MIN_EXCEEDS_DEFINED_CHOICES,
    REQUIRED_GROUPS_EXCEED_CEILING,
    inspect_modifier_definition,
)
from restaurants_app.models import MenuItem

EXIT_CLEAN = 0
EXIT_BLOCKER = 1
EXIT_CONCERNS = 2
EXIT_INCOMPLETE = 3

#: An extras minimum above this cannot be submitted, so the item is unorderable.
EXTRAS_MIN_EXCEEDS_CEILING = 'extras_min_exceeds_ceiling'

DEFAULT_SAMPLE_SIZE = 20
MAX_SAMPLE_SIZE = 200
DEFAULT_CHUNK_SIZE = 500


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
    def _inspect_catalogue(self, sample_size, chunk_size):
        invalid = _Bucket(sample_size)
        invalid_inactive = _Bucket(sample_size)
        min_over_choices = _Bucket(sample_size)
        min_over_ceiling = _Bucket(sample_size)
        required_groups_over = _Bucket(sample_size)
        extras_min_over = _Bucket(sample_size)
        wide_groups = _Bucket(sample_size)
        wide_choices = _Bucket(sample_size)

        scanned = 0
        rows = (
            MenuItem.objects
            .only(
                'id', 'options', 'approved', 'enabled', 'deleted',
                'has_extras', 'extras_min_selections',
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
            )

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
                if verdict.group_count > MAX_MODIFIER_GROUPS_PER_LINE:
                    wide_groups.add(item.pk, f'{verdict.group_count} groups')
                if verdict.max_choices_in_a_group > MAX_CHOICES_PER_GROUP:
                    wide_choices.add(
                        item.pk,
                        f'{verdict.max_choices_in_a_group} choices in a group',
                    )

            # Extras are a separate axis from modifier groups.
            minimum = item.extras_min_selections or 0
            if item.has_extras and minimum > MAX_EXTRAS_PER_LINE:
                extras_min_over.add(item.pk, state)

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
        ]
        return concerns, informational, scanned
