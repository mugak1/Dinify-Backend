"""
Test helpers for the admin control plane.

``AuditAssertionsMixin`` is the audit RATCHET: because audit capture is an explicit
call rather than automatic middleware, the thing that stops a future endpoint from
silently forgetting it is a test that asserts the entry exists. Endpoint tests in
PR-2b and later mix this in and call ``assertAudited(...)``.

``give_legacy_platform_role`` is the counterpart for the customer plane: several
suites need an account that CARRIES the retired ``dinify_admin`` string in order to
assert it grants nothing. Every write path refuses the string now, so the only way
to produce such a row is to go around them — done in one reviewed place rather than
re-invented per suite.

Kept dependency-light on purpose — it uses only the ``unittest`` assertions the
host TestCase already provides, so importing it costs nothing at runtime.
"""
from platform_admin_app.models import AdminAuditLog

# The retired platform role. Lives here, beside the invariant service that owns the
# denylist, so no customer-plane module has to spell it. Tests assert this string
# is INERT — it is never a capability.
LEGACY_PLATFORM_ROLE = 'dinify_admin'


def give_legacy_platform_role(user, role=LEGACY_PLATFORM_ROLE):
    """
    Force the retired platform role onto ``user``, bypassing every write guard.

    A deliberate end-run around ``assert_no_platform_roles``: the question these
    tests ask is what a row that ALREADY carries the string can do — a legacy row,
    a stray shell session, a bad migration — not whether one can be created through
    the API. Refreshes and returns the user.
    """
    from users_app.models import User

    User.objects.filter(pk=user.pk).update(roles=[role])
    user.refresh_from_db()
    return user


class AuditAssertionsMixin:
    """Assertions over ``AdminAuditLog`` for endpoint and service tests."""

    def assertAudited(self, action, *, count=1, **filters):
        """
        Assert exactly ``count`` audit entries match ``action`` + ``filters``.

        Any model field works as a filter keyword (``result=``, ``actor=``,
        ``resource_id=``, ``restaurant_id=``…). Returns the matching entry when
        ``count == 1`` so the caller can make further assertions about it, else the
        queryset.
        """
        queryset = AdminAuditLog.objects.filter(action=action, **filters)
        actual = queryset.count()
        if actual != count:
            existing = list(
                AdminAuditLog.objects.values_list('action', 'result')
            )
            raise AssertionError(
                f'Expected {count} audit entry/entries for action={action!r} '
                f'with {filters!r}, found {actual}. All entries: {existing!r}'
            )
        return queryset.first() if count == 1 else queryset

    def assertNotAudited(self, action, **filters):
        """Assert no audit entry matches — e.g. an unaudited liveness route."""
        return self.assertAudited(action, count=0, **filters)
