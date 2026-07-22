"""
Regression guard for PR-0B: the User archival post_save signal (`archive_user`)
and its `fields='__all__'` serializer (`SerArcUser`) were removed because they
shipped the full User row — password hash included — to MongoDB on every save.
These tests fail if either is reintroduced.
"""
import inspect

from django.test import TestCase
from django.db.models.signals import post_save

from users_app.models import User
from dinify_backend.tenancy.discovery import all_project_serializers


class NoUserArchivalSignalTests(TestCase):
    def test_no_archival_post_save_receiver_on_user(self):
        # Django 5.2 `_live_receivers` returns a (sync, async) pair of lists of
        # already-resolved callables and includes sender=None receivers — exactly
        # the set that would fire on a User.save(). The isinstance guard keeps this
        # working if the pinned Django ever changes the return shape.
        live = post_save._live_receivers(User)
        receivers = (
            [fn for group in live for fn in group]
            if isinstance(live, tuple)
            else list(live)
        )

        offending = []
        for fn in receivers:
            module = getattr(fn, "__module__", "") or ""
            qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", "")) or ""
            label = f"{module}.{qualname}"
            if "archive" in label.lower():
                offending.append(label)
                continue
            try:
                source = inspect.getsource(fn)
            except (OSError, TypeError):
                source = ""
            if "archive_record" in source:
                offending.append(label)

        self.assertEqual(
            offending, [],
            f"Archival post_save receiver(s) still registered on User: {offending}. "
            "The archive_user signal must stay deleted (PR-0B).",
        )


class NoReadableUserPasswordSerializerTests(TestCase):
    def test_no_user_serializer_exposes_readable_password(self):
        user_serializers = [
            cls for cls in all_project_serializers()
            if getattr(getattr(cls, "Meta", None), "model", None) is User
        ]

        # Guard against a vacuous pass: at least one project User ModelSerializer
        # (today, SerGetUserProfile) must be discovered, else the assertion below
        # would silently protect nothing.
        self.assertTrue(
            user_serializers,
            "No project ModelSerializer with Meta.model=User was discovered — "
            "discovery may be broken; the password assertion would be vacuous.",
        )

        for cls in user_serializers:
            field = cls().fields.get("password")
            self.assertTrue(
                field is None or field.write_only,
                f"{cls.__module__}.{cls.__qualname__} exposes a readable "
                "'password' field (must be absent or write_only=True).",
            )
