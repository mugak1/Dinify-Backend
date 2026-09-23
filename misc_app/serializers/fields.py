"""
Shared custom serializer fields.
"""
import json

from django.db import models
from rest_framework import serializers
from rest_framework.settings import api_settings


class JSONStringCompatField(serializers.JSONField):
    """
    JSONField that accepts stringified JSON (as arrives via multipart/form-data)
    and parses it before delegating to the base JSONField. Non-string inputs
    pass through unchanged so JSON-body callers are unaffected.
    """

    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, json.JSONDecodeError):
                self.fail('invalid')
        return super().to_internal_value(data)


class JSONStringCompatListField(serializers.ListField):
    """
    ListField that accepts a stringified JSON array (as arrives via
    multipart/form-data, e.g. "[]" or '["<uuid>", ...]') and parses it
    before delegating to the base ListField. Non-string inputs pass
    through unchanged so JSON-body callers are unaffected.
    """

    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, json.JSONDecodeError):
                self.fail('not_a_list', input_type=type(data).__name__)
        return super().to_internal_value(data)


class _MediaPathRepresentation:
    """
    Render an uploaded file as its MEDIA path (``/media/menu_items/x.webp``), never
    as an absolute URL, WHATEVER the serializer context carries.

    **THIS IS THE WIRE CONTRACT EVERY CLIENT IS BUILT ON.** The restaurant portal,
    the diner app and the dashboard all render ``environment.apiUrl + image``, and
    the deployed ``apiUrl`` carries the Apache mount (``https://…/uat``), which is
    also where the media alias lives (``/uat/media/``).

    DRF's stock ``FileField`` does something else the moment a ``request`` is in
    the context: it returns ``request.build_absolute_uri(value.url)``. That breaks
    twice over. The client prepends ``apiUrl`` to a URL that is already absolute,
    producing ``https://…/uathttps://…/media/…``. And the absolute URL is wrong on
    its own: ``MEDIA_URL`` is ``/media/`` with a leading slash, which Django never
    prefixes with ``SCRIPT_NAME``, so it names ``/media/…`` at the host root, where
    no alias exists.

    That stayed invisible only because most reads handed their serializer NO
    context. ``Secretary._read_context`` (the QR-disclosure fix) rightly started
    passing the request, and every image on the portal's menu read broke with it.
    So the rule lives HERE, on the field, and does not depend on which context a
    caller happens to pass. The request must stay in the context, because the QR
    credential policy reads it.

    Semantics otherwise mirror DRF exactly: a falsy value is ``None``,
    ``use_url=False`` yields the stored name, and a value with no ``url`` is
    ``None``. Input parsing and validation are inherited unchanged.
    """

    def to_representation(self, value):
        if not value:
            return None
        if not getattr(self, 'use_url', api_settings.UPLOADED_FILES_USE_URL):
            return value.name
        try:
            return value.url
        except AttributeError:
            return None


class MediaPathFileField(_MediaPathRepresentation, serializers.FileField):
    """``FileField`` that always renders the media path. See ``_MediaPathRepresentation``."""


class MediaPathImageField(_MediaPathRepresentation, serializers.ImageField):
    """``ImageField`` that always renders the media path. See ``_MediaPathRepresentation``."""


class MediaPathFieldsMixin:
    """
    Map every model file/image column a ``ModelSerializer`` builds onto the
    media-path fields above.

    Put it BEFORE ``ModelSerializer`` in the bases. It changes only how an
    auto-built field RENDERS. A field declared explicitly on the class must use
    ``MediaPathImageField`` / ``MediaPathFileField`` itself.
    ``restaurants_app.tests_media_paths`` checks every project serializer for
    both.
    """

    serializer_field_mapping = {
        **serializers.ModelSerializer.serializer_field_mapping,
        models.FileField: MediaPathFileField,
        models.ImageField: MediaPathImageField,
    }
