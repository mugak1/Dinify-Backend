"""
The REASONED ADMIN REQUEST contract — one implementation, shared by every elevated
control-plane write that requires an operator to say why.

It lives in its own module rather than beside any one surface because it belongs to
none of them: a substantive reason, the audit error vocabulary derived from DRF's own
codes, and the rule about which reason a REJECTED request is allowed to record are
properties of *an audited admin write*, not of commercial configuration or of
onboarding.

Both rules below were real defects found in review on the first surface that needed
them, which is exactly why there must not be a second copy: a copy would be a second
place for the same two mistakes to come back.
"""
from rest_framework import serializers
from rest_framework.exceptions import ParseError, UnsupportedMediaType

from platform_admin_app.delegation import MIN_REASON_LENGTH

# A generous upper bound on the stated reason. Not a product rule — a bound, so one
# caller cannot push an unbounded blob into the audit table. No honest operator
# explanation reaches it.
MAX_REASON_LENGTH = 1000

# --- the reason contract -----------------------------------------------------

class ReasonedRequestSerializer(serializers.Serializer):
    """
    Base for every reasoned admin request body: a substantive reason, plus the audit
    vocabulary derived from whatever else failed.

    Plain ``Serializer``, never ``ModelSerializer``. A model-bound serializer over a
    control-plane table would expose its attribution columns to DRF's generic
    mutation machinery and let a request body name fields the control plane is
    supposed to stamp from the authenticated session.
    """

    reason = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=MAX_REASON_LENGTH,
        # DRF's default, restated because it is load-bearing here: a whitespace-only
        # reason trims to '' and is then refused by `allow_blank=False`, and the
        # value that reaches the audit row is the trimmed one.
        trim_whitespace=True,
    )

    # Subclasses override to name their own fields FIRST. Fixed order, so the audit
    # error code for a body with several problems is deterministic rather than
    # dependent on dict iteration.
    _FIELD_ORDER = ('reason',)

    def validate_reason(self, value):
        """
        The house reason bar, imported rather than respelled.

        ``MIN_REASON_LENGTH`` comes from ``platform_admin_app.delegation``, which
        ``restaurants_app.controllers.lifecycle`` already mirrors — a reason is a
        reason, and a further standard on a further surface is how they start
        disagreeing. The messages match those surfaces too.
        """
        if len(value) < MIN_REASON_LENGTH:
            raise serializers.ValidationError(
                f'Please state a reason of at least {MIN_REASON_LENGTH} characters.',
                code='too_short',
            )
        return value

    def audit_reason(self):
        """
        The reason to record on a REJECTED request: normalized, or empty.

        A reason is recorded ONLY when the reason field itself validated. Reading the
        raw ``initial_data`` instead — the first version of this did — records two
        wrong things: a reason that was itself rejected for being too short still
        lands in the log as though it were a stated reason, and a valid but padded
        one is stored untrimmed whenever some OTHER field is what failed.

        DRF mechanics matter here. ``validated_data`` is ``{}`` after ANY failure, so
        it cannot supply the surviving field's value. What it does give is
        ``self.errors``: if ``reason`` is absent from it, both the field's own
        validation and ``validate_reason`` succeeded — so re-running the field
        reproduces exactly the value the serializer would have kept, deterministically
        and with no second normalisation rule.
        """
        if not isinstance(self.initial_data, dict) or 'reason' in self.errors:
            return ''
        field = self.fields['reason']
        try:
            return field.run_validation(field.get_value(self.initial_data))
        except serializers.ValidationError:
            # Unreachable given the `errors` check above; fail closed anyway rather
            # than let an audit write raise.
            return ''

    def audit_error_code(self):
        """
        One stable machine code naming the first problem, for the audit row.

        Built from DRF's own error codes so it stays in step with the validation
        rather than being a parallel vocabulary maintained by hand: a missing reason
        becomes ``reason_required`` and a short one ``reason_too_short``, which is
        exactly what the delegation and lifecycle surfaces already emit.
        """
        for field in self._FIELD_ORDER:
            detail = self.errors.get(field)
            if not detail:
                continue
            code = getattr(detail[0], 'code', '') if isinstance(detail, list) else ''
            return f'{field}_{code}' if code else f'invalid_{field}'
        return 'invalid_request'


# --- the guarded body parse --------------------------------------------------

# Error codes for a body the server could not read at all. Enumerable, like every
# other admin vocabulary, so a client can branch on them.
MALFORMED_BODY = 'malformed_body'
UNSUPPORTED_MEDIA_TYPE = 'unsupported_media_type'


def read_request_body(request):
    """
    ``(payload, unreadable)`` — exactly one is ``None``.

    ``unreadable`` is a ``(http_status, error_code, detail)`` triple. The CALLER
    audits it and builds the response, because only the caller knows which resource
    the request was aimed at (and, for a creation, that there is not one yet). What
    lives here is the classification, which is the part that was a real defect.

    ``request.data`` PARSES ON ACCESS, and a body DRF cannot read raises at that
    access rather than reaching a serializer. Left unguarded, DRF answers with its own
    bare ``{"detail": ...}`` — a different shape from every other error these
    endpoints return — and, more importantly, the request never reaches ``audit``, so
    an elevated administrator's unsafe request would be absent from the control-plane
    log purely because it was unreadable.

    TWO DISTINCT EXCEPTIONS REACH HERE, and catching only the first is the easy
    mistake: malformed JSON raises ``ParseError``, but a ``Content-Type`` with no
    parser at all raises ``UnsupportedMediaType``, which is NOT a subclass of it. Both
    are "the server could not read this request", so both must be audited — but they
    are answered differently, because the STATUS is the caller's remedy. A ``400``
    says *the body was wrong*; a ``415`` says *send JSON*. Folding the second into the
    first would delete the one clue that tells the operator which mistake they made.

    (An EMPTY body never reaches either branch — DRF invokes a parser only when there
    is content — so an empty ``text/plain`` request is an ordinary validation failure,
    not a media-type one.)

    The exception detail describes the CALLER'S OWN input and carries no server state,
    so it is passed back for the caller to surface: hiding it would cost an operator
    the one clue they need without protecting anything.
    """
    try:
        return request.data, None
    except UnsupportedMediaType as exc:
        return None, (415, UNSUPPORTED_MEDIA_TYPE, str(exc.detail))
    except ParseError as exc:
        return None, (400, MALFORMED_BODY, str(exc.detail))
