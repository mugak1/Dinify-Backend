"""
Test helpers for the admin control plane.

``AuditAssertionsMixin`` is the audit RATCHET: because audit capture is an explicit
call rather than automatic middleware, the thing that stops a future endpoint from
silently forgetting it is a test that asserts the entry exists. Endpoint tests in
PR-2b and later mix this in and call ``assertAudited(...)``.

Kept dependency-light on purpose — it uses only the ``unittest`` assertions the
host TestCase already provides, so importing it costs nothing at runtime.
"""
from platform_admin_app.models import AdminAuditLog


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
