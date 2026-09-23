"""
MEDIA LEAVES THE API AS A PATH, WHATEVER THE SERIALIZER CONTEXT CARRIES.

Every client renders ``environment.apiUrl + image``, and the deployed ``apiUrl``
carries the Apache mount (``https://api-test.dinifyapp.com/uat``), which is also
where the media alias lives. DRF's stock file field returns
``request.build_absolute_uri(url)`` instead as soon as a ``request`` is in the
context, and ``Secretary._read_context`` (the QR-disclosure fix) started passing
one. Every image on the restaurant portal's menu read broke at once. The client
built ``…/uathttps://api-test.dinifyapp.com/media/…``, and the absolute URL is
wrong even taken on its own: ``MEDIA_URL`` is ``/media/``, which Django never
prefixes with ``SCRIPT_NAME``, so it names a path with no alias behind it.

The reads here run the way the box serves them: over HTTPS, under
``SCRIPT_NAME='/uat'``, on the public host. That is the only shape in which an
absolute URL and a path look different, which is what lets these tests tell the
defect apart from the fix.
"""
import io
import shutil
import tempfile
from types import SimpleNamespace

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from PIL import Image
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from dinify_backend.tenancy.discovery import all_project_serializers
from misc_app.serializers.fields import (
    MediaPathFileField, MediaPathImageField, _MediaPathRepresentation,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

RESTAURANT_OWNER = 'owner'
HOST = 'api-test.dinifyapp.com'
MOUNT = '/uat'
SETUP = '/api/v1/restaurant-setup/'


def _jpeg(name):
    buffer = io.BytesIO()
    Image.new('RGB', (40, 40), color=(200, 60, 60)).save(buffer, 'JPEG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/jpeg')


def _is_absolute(value):
    return isinstance(value, str) and value.startswith(('http://', 'https://', '//'))


class _TempMedia:
    def setUp(self):
        self._media = tempfile.mkdtemp(prefix='dinify_media_paths_')
        self._media_override = override_settings(MEDIA_ROOT=self._media)
        self._media_override.enable()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._media_override.disable()
        shutil.rmtree(self._media, ignore_errors=True)


class PortalReadsUnderTheDeployedMountTests(_TempMedia, TestCase):
    """THE REGRESSIONS: the three restaurant-setup reads that carry an image."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.owner = User.objects.create_user(
            first_name='O', last_name='W', email='media-owner@t.com',
            phone_number='256700310001', username='256700310001',
            country='Uganda', password='x', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Media R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live,
            logo=_jpeg('logo.jpg'), cover_photo=_jpeg('cover.jpg'),
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        # MenuSection/MenuItem.save() re-encode an upload as WebP, so the stored
        # names are read back from the rows rather than assumed.
        self.section = MenuSection.objects.create(
            name='Breakfast', restaurant=self.restaurant,
            approved=True, enabled=True,
            section_banner_image=_jpeg('banner.jpg'),
        )
        self.item = MenuItem.objects.create(
            name='Avocado Toast', section=self.section, primary_price=10000,
            image=_jpeg('avocado.jpg'),
        )
        self.section.refresh_from_db()
        self.item.refresh_from_db()
        self.restaurant.refresh_from_db()

    def _read(self, record, **params):
        token = str(RefreshToken.for_user(self.owner).access_token)
        response = self.client.get(
            f'{SETUP}{record}/', params,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            HTTP_HOST=HOST, SCRIPT_NAME=MOUNT, secure=True,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['records']

    def test_THE_REGRESSION_a_menu_item_image_is_the_media_path(self):
        [row] = self._read('menuitems', section=str(self.section.id))
        self.assertTrue(self.item.image.name.startswith('menu_items/'))
        self.assertEqual(row['image'], f'/media/{self.item.image.name}')
        self.assertFalse(_is_absolute(row['image']), row['image'])

    def test_THE_REGRESSION_a_section_banner_is_the_media_path(self):
        [row] = self._read('menusections', restaurant=str(self.restaurant.id))
        self.assertEqual(
            row['section_banner_image'],
            f'/media/{self.section.section_banner_image.name}')

    def test_THE_REGRESSION_the_restaurant_logo_and_cover_are_media_paths(self):
        rows = self._read('restaurants')
        [row] = [r for r in rows if r['id'] == str(self.restaurant.id)]
        self.assertEqual(row['logo'], f'/media/{self.restaurant.logo.name}')
        self.assertEqual(
            row['cover_photo'], f'/media/{self.restaurant.cover_photo.name}')

    def test_an_item_without_an_image_is_still_null(self):
        MenuItem.objects.create(
            name='Plain', section=self.section, primary_price=5000)
        rows = self._read('menuitems', section=str(self.section.id))
        by_name = {r['name']: r for r in rows}
        self.assertIsNone(by_name['Plain']['image'])

    def test_CONTROL_the_request_still_reaches_the_serializer_context(self):
        # The wrong fix is to stop passing the request. The QR credential policy
        # needs it, so the owner's table read must still carry a credential.
        area = DiningArea.objects.create(name='Main', restaurant=self.restaurant)
        Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        [row] = self._read('tables', restaurant=str(self.restaurant.id))
        credential = row.get('qr_credential')
        self.assertTrue(isinstance(credential, str) and credential, row)


class MediaPathFieldTests(SimpleTestCase):
    """The field itself, against DRF's stock behaviour as a negative control."""

    value = SimpleNamespace(name='menu_items/x.webp', url='/media/menu_items/x.webp')

    def _context(self):
        raw = RequestFactory().get(
            '/api/v1/restaurant-setup/menuitems/',
            HTTP_HOST=HOST, SCRIPT_NAME=MOUNT, secure=True,
        )
        return {'request': Request(raw)}

    def _bind(self, field, context):
        parent = serializers.Serializer(context=context)
        field.bind('image', parent)
        return field

    def test_CONTROL_the_stock_field_turns_the_same_request_absolute(self):
        # Proves the request here is one DRF would use, so the next test is
        # not passing because the context was inert.
        stock = self._bind(serializers.ImageField(), self._context())
        self.assertTrue(_is_absolute(stock.to_representation(self.value)))

    def test_a_request_in_the_context_does_not_make_it_absolute(self):
        for cls in (MediaPathImageField, MediaPathFileField):
            field = self._bind(cls(), self._context())
            self.assertEqual(
                field.to_representation(self.value), '/media/menu_items/x.webp')

    def test_without_a_request_it_is_what_it_always_was(self):
        field = self._bind(MediaPathImageField(), {})
        stock = self._bind(serializers.ImageField(), {})
        self.assertEqual(
            field.to_representation(self.value),
            stock.to_representation(self.value))

    def test_the_remaining_drf_semantics_are_unchanged(self):
        field = self._bind(MediaPathImageField(), self._context())
        self.assertIsNone(field.to_representation(None))
        self.assertIsNone(field.to_representation(''))
        by_name = self._bind(MediaPathImageField(use_url=False), self._context())
        self.assertEqual(by_name.to_representation(self.value), 'menu_items/x.webp')
        no_url = SimpleNamespace(name='x')
        self.assertIsNone(field.to_representation(no_url))


class EveryProjectSerializerRendersMediaAsAPathTests(SimpleTestCase):
    """
    THE GUARD. Any serializer that builds a file field must build a media-path
    one, whether it is auto-mapped from a model column or declared on the class.
    The next serializer handed a request cannot reopen this.
    """

    # The fields the audit found when this was written. Asserting the discovered
    # set covers them stops the guard passing vacuously if discovery breaks.
    KNOWN = frozenset({
        'restaurants_app.models.SerArcMenuItem::image',
        'restaurants_app.models.SerArcMenuSection::section_banner_image',
        'restaurants_app.models.SerArcRestaurant::logo',
        'restaurants_app.models.SerArcRestaurant::cover_photo',
        'restaurants_app.serializers.SerializerGetFullMenu::section_banner_image',
        'restaurants_app.serializers.SerializerGetRestaurantDetail::logo',
        'restaurants_app.serializers.SerializerGetRestaurantDetail::cover_photo',
        'restaurants_app.serializers.SerializerPublicGetMenuItem::image',
        'restaurants_app.serializers.SerializerPublicGetMenuSection::section_banner_image',
        'restaurants_app.serializers.SerializerPublicGetRestaurant::logo',
        'restaurants_app.serializers.SerializerPublicGetRestaurant::cover_photo',
        'restaurants_app.serializers.SerializerPutMenuItem::image',
        'restaurants_app.serializers.SerializerPutMenuSection::section_banner_image',
        'restaurants_app.serializers.SerializerPutRestaurant::logo',
        'restaurants_app.serializers.SerializerPutRestaurant::cover_photo',
        'restaurants_app.serializers.UpsellItemSerializer::item_image',
    })

    def _file_fields(self):
        found = {}
        for cls in all_project_serializers():
            for name, field in cls().fields.items():
                if isinstance(field, serializers.FileField):
                    found[f'{cls.__module__}.{cls.__qualname__}::{name}'] = field
        return found

    def test_the_audit_is_still_covered(self):
        missing = self.KNOWN - set(self._file_fields())
        self.assertFalse(missing, f'discovery no longer sees: {sorted(missing)}')

    def test_every_file_field_renders_the_media_path(self):
        offenders = sorted(
            key for key, field in self._file_fields().items()
            if not isinstance(field, _MediaPathRepresentation)
        )
        self.assertEqual(
            offenders, [],
            'these serializers render an ABSOLUTE media URL once a request is in '
            'their context. Add MediaPathFieldsMixin before ModelSerializer, or '
            'declare the field as MediaPathImageField / MediaPathFileField.')
