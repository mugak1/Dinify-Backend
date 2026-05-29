"""
Shared custom serializer fields.
"""
import json

from rest_framework import serializers


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
