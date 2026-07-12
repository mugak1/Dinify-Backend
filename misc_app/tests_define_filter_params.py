"""
Unit tests for define_filter_params — it must never 500 on an unknown query
param or an unregistered model (BUG-P2-3f/g).

Before the fix the function did a bare dict subscript
`filter_considerations[key.lower()]` (KeyError on any param absent from the
model's map) after `FILTER_DEFINITIONS.get(model)` (None -> TypeError for a
model not in the map). Both surfaced as HTTP 500 on the list endpoints that
call it. These tests lock in the graceful-skip behaviour.
"""
from django.http import QueryDict
from django.test import TestCase

from misc_app.controllers.define_filter_params import (
    FILTER_DEFINITIONS,
    define_filter_params,
)


class DefineFilterParamsTests(TestCase):
    """define_filter_params maps known params and skips everything else."""

    def test_known_param_maps_to_orm_filter(self):
        result = define_filter_params({'name': 'Pizza'}, 'restaurants')
        self.assertEqual(result, {'name__icontains': 'Pizza'})

    def test_unknown_param_is_skipped_not_crashed(self):
        # Any param absent from the model's map used to raise KeyError -> 500.
        result = define_filter_params({'foo': 'barbar'}, 'restaurants')
        self.assertEqual(result, {})

    def test_unknown_model_returns_empty_dict(self):
        # A model not registered in FILTER_DEFINITIONS used to raise TypeError
        # (None[...]) -> 500; it must now resolve to an empty filter map.
        self.assertNotIn('widgets', FILTER_DEFINITIONS)
        result = define_filter_params({'foo': 'barbar'}, 'widgets')
        self.assertEqual(result, {})

    def test_mixed_known_and_unknown_applies_only_known(self):
        result = define_filter_params(
            {'name': 'Pizza', 'foo': 'barbar'}, 'restaurants',
        )
        self.assertEqual(result, {'name__icontains': 'Pizza'})

    def test_pagination_keys_are_skipped(self):
        # page/page_size are dropped by the explicit skip, not the length guard
        # (page_size='50' has length > 1), leaving only the real filter.
        result = define_filter_params(
            {'page': '2', 'page_size': '50', 'name': 'Pizza'}, 'restaurants',
        )
        self.assertEqual(result, {'name__icontains': 'Pizza'})

    def test_querydict_input_is_supported(self):
        # Real callers pass request.GET, a QueryDict.
        params = QueryDict(mutable=True)
        params['name'] = 'Pizza'
        params['foo'] = 'barbar'
        result = define_filter_params(params, 'restaurants')
        self.assertEqual(result, {'name__icontains': 'Pizza'})

    def test_single_character_value_is_dropped_preexisting_quirk(self):
        # Documents (does NOT change) the `len(value) > 1` guard: a single-
        # character value is silently skipped. Pre-existing, out of scope here.
        result = define_filter_params({'name': 'P'}, 'restaurants')
        self.assertEqual(result, {})
