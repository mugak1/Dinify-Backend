"""
the serializers for the restaurant app
"""
import json
import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order
from rest_framework import serializers
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table,
    SectionGroup, DiningArea, UpsellConfig, UpsellItem,
    Reservation, WaitlistEntry, RestaurantTag
)
from misc_app.serializers.fields import JSONStringCompatField, JSONStringCompatListField
from restaurants_app.controllers.tables import get_table_availability


class SerializerGetRestaurantDetail(ModelSerializer):
    class Meta:
        """
        the meta class for the serializers
        """
        model = Restaurant
        fields = '__all__'


class SerializerPutRestaurant(ModelSerializer):
    """
    serializer for adding and editing restaurant details
    """
    class Meta:
        """
        the meta class for the serializers
        """
        model = Restaurant
        fields = '__all__'


class SerializerPublicGetRestaurant(ModelSerializer):
    """
    serializer for getting public restaurant details
    """
    owner = SerializerMethodField()

    class Meta:
        model = Restaurant
        fields = (
            "id", "name", "location",
            "logo", "cover_photo", "status", "owner",
            "preset_tags"
        )

    def get_owner(self, restaurant):
        return {
            'id': str(restaurant.owner.pk),
            'first_name': restaurant.owner.first_name,
            'last_name': restaurant.owner.last_name,
            'email': restaurant.owner.email,
            'phone': restaurant.owner.phone_number
        }


class SerializerMiscPublicRestaurant(ModelSerializer):
    """
    public restaurant listing for the AllowAny misc-public endpoint.

    Deliberately excludes the owner block: a public listing exposes the
    restaurant, never the owner's personal PII (name/email/phone).
    """
    class Meta:
        model = Restaurant
        fields = (
            "id", "name", "location",
            "logo", "cover_photo", "status",
            "preset_tags",
        )


class SerializerEmployeeGetRestaurant(ModelSerializer):
    """
    serializer for getting restaurant details for the employee
    """
    class Meta:
        model = RestaurantEmployee
        fields = ("id", "name", "location")


class SerializerPutRestaurantEmployee(ModelSerializer):
    """
    serializer for adding and editing restaurant employees
    """
    class Meta:
        model = RestaurantEmployee
        fields = '__all__'


class SerializerGetRestaurantEmployee(ModelSerializer):
    """
    serializer for getting restaurant employees
    """
    name = SerializerMethodField()
    user = SerializerMethodField()

    class Meta:
        model = RestaurantEmployee
        fields = (
            "id", "time_created", "time_last_updated",
            "name", "roles", "active",
            "user"
        )

    def get_name(self, employee):
        """
        returns the name of the employee
        """
        return f"{employee.user.first_name} {employee.user.last_name}"

    def get_user(self, employee):
        """
        returns the user details of the employee
        """
        return {
            'id': str(employee.user.pk),
            'first_name': employee.user.first_name,
            'last_name': employee.user.last_name,
            'email': employee.user.email,
            'phone_number': employee.user.phone_number,
        }


class SerializerPutMenuSection(ModelSerializer):
    """
    serializer for adding menu section
    """
    schedules = JSONStringCompatField(required=False)

    class Meta:
        model = MenuSection
        fields = '__all__'


class SerializerPublicGetMenuSection(ModelSerializer):
    """
    serializer for getting the menu section
    """
    item_count = SerializerMethodField()
    has_groups = SerializerMethodField()
    groups = SerializerMethodField()

    class Meta:
        model = MenuSection
        fields = (
            'id', 'name', 'description', 'section_banner_image',
            'available', 'availability', 'schedules',
            'item_count', 'has_groups', 'groups',
            'listing_position'
        )

    def get_item_count(self, menu_section):
        return MenuItem.objects.filter(
            section=menu_section,
            # section_group__deleted=False,
            # section_group__available=True,
            deleted=False
        ).count()

    def get_has_groups(self, menu_section):
        return SectionGroup.objects.filter(
            section=menu_section,
            deleted=False
        ).count() > 0

    def get_groups(self, menu_section):
        groups = SectionGroup.objects.filter(
            section=menu_section,
            deleted=False,
            # available=True
        )
        return [
            {
                'id': str(group.pk),
                'name': group.name,
                'available': group.available
            } for group in groups
        ]


class SerializerPutSectionGroup(ModelSerializer):
    class Meta:
        model = SectionGroup
        fields = '__all__'


class SerializerPublicGetSectionGroup(ModelSerializer):
    item_count = SerializerMethodField()

    class Meta:
        model = SectionGroup
        fields = ('id', 'name', 'item_count',)

    def get_item_count(self, group):
        return MenuItem.objects.filter(
            section_group=group,
            section_group__deleted=False,
            section_group__available=True,
            deleted=False,
        ).count()


class SerializerPutMenuItem(ModelSerializer):
    """
    serializer for adding menu Item
    """
    options = JSONStringCompatField(required=False)
    allergens = JSONStringCompatField(required=False)
    discount_details = JSONStringCompatField(required=False)
    extras_applicable = JSONStringCompatField(required=False)
    tag_ids = JSONStringCompatListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = MenuItem
        fields = '__all__'

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # Light guard: a single global extras selection limit (min <= max).
        # Runs on every PUT — placed before the tag_ids early-return below so
        # extras-only updates are still validated.
        emin = attrs.get('extras_min_selections')
        emax = attrs.get('extras_max_selections')
        if emin is not None and emax not in (None, 0) and emin > emax:
            raise serializers.ValidationError(
                {'extras_min_selections': 'Minimum extras cannot exceed maximum extras.'})

        # Defense-in-depth contract parity with the admin discounts form (PR #480):
        # reject a clearly-inverted date window. Placed before the tag_ids
        # early-return so a discount-only PUT is still validated. end == start is
        # a valid one-day window (strict '<'). Only fires on a well-formed window —
        # unparseable dates are left to is_discount_active() (which treats them as
        # no bound). A fully-past window is NOT rejected (the form only warns).
        discount_details = attrs.get('discount_details')
        if isinstance(discount_details, dict):
            start_raw = discount_details.get('start_date') or ''
            end_raw = discount_details.get('end_date') or ''
            if start_raw and end_raw:
                try:
                    start_date = datetime.strptime(start_raw, '%Y-%m-%d').date()
                    end_date = datetime.strptime(end_raw, '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    start_date = end_date = None
                if start_date and end_date and end_date < start_date:
                    raise serializers.ValidationError(
                        {'discount_details': 'End date must be on or after the start date.'})

        tag_ids = attrs.get('tag_ids')
        if tag_ids is None:
            return attrs

        # Resolve the restaurant from the section (in attrs for create,
        # in self.instance for update). All supplied tag IDs must belong
        # to that restaurant — cross-tenant references are rejected.
        section = attrs.get('section')
        if section is None and self.instance is not None:
            section = self.instance.section
        if section is None:
            raise serializers.ValidationError({
                'tag_ids': 'Cannot apply tags without a section.'
            })

        section_id = getattr(section, 'id', section)
        try:
            restaurant_id = MenuSection.objects.values(
                'restaurant_id'
            ).get(id=section_id)['restaurant_id']
        except MenuSection.DoesNotExist:
            raise serializers.ValidationError({
                'tag_ids': 'Section does not exist.'
            })

        unique_ids = list({str(tid) for tid in tag_ids})
        if unique_ids:
            valid_count = RestaurantTag.objects.filter(
                restaurant_id=restaurant_id,
                id__in=unique_ids,
                deleted=False,
            ).count()
            if valid_count != len(unique_ids):
                raise serializers.ValidationError({
                    'tag_ids': (
                        'One or more tag IDs do not belong to this '
                        'restaurant or do not exist.'
                    )
                })
        return attrs

    def create(self, validated_data):
        tag_ids = validated_data.pop('tag_ids', None)
        instance = super().create(validated_data)
        if tag_ids is not None:
            instance.sync_tag_links(tag_ids)
        return instance

    def update(self, instance, validated_data):
        tag_ids = validated_data.pop('tag_ids', None)
        instance = super().update(instance, validated_data)
        if tag_ids is not None:
            instance.sync_tag_links(tag_ids)
        return instance


class SerializerRestaurantTag(ModelSerializer):
    """Serializer for the restaurant-scoped tag catalog."""

    class Meta:
        model = RestaurantTag
        fields = (
            'id', 'restaurant', 'name', 'category',
            'icon', 'colour', 'filterable',
            'display_order', 'is_system_preset',
        )
        read_only_fields = ('is_system_preset',)


class SerializerPublicGetMenuItem(ModelSerializer):
    """
    serializer for getting the menu Item
    """
    has_options = SerializerMethodField()
    group = SerializerMethodField()
    extras = SerializerMethodField()
    discount_percentage = SerializerMethodField()
    is_discount_active = SerializerMethodField()
    current_price = SerializerMethodField()
    tags = SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = (
            'id', 'name', 'description', 'calories', 'primary_price',
            'discounted_price', 'running_discount', 'image',
            'available', 'in_stock', 'allergens', 'tags', 'discount_details',
            'has_options', 'options', 'section', 'group', 'extras', 'is_extra',
            'discount_percentage', 'is_discount_active', 'current_price',
            'has_extras', 'is_special',
            'is_featured', 'is_popular', 'is_new',
            'age_restricted', 'extras_min_selections', 'extras_max_selections'
        )

    def get_tags(self, menu_item):
        tags = menu_item.tags.all().order_by('display_order', 'name')
        return [
            {
                'id': str(tag.id),
                'name': tag.name,
                'category': tag.category,
                'icon': tag.icon,
                'colour': tag.colour,
            }
            for tag in tags
        ]

    def get_has_options(self, menu_item):
        options = menu_item.options
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (ValueError, TypeError):
                return False
        if not isinstance(options, dict):
            return False
        return bool(options.get('hasModifiers'))

    def get_group(self, menu_item):
        if menu_item.section_group is None:
            return None
        if menu_item.section_group.deleted:
            return None
        return {
            'id': str(menu_item.section_group.pk),
            'name': menu_item.section_group.name
        }

    def get_extras(self, menu_item):
        applicable_extras = menu_item.extras_applicable
        if not applicable_extras:
            return []
        if isinstance(applicable_extras, str):
            try:
                applicable_extras = json.loads(applicable_extras)
            except (ValueError, TypeError):
                return []
        if not isinstance(applicable_extras, list):
            return []
        extras = []
        for extra in applicable_extras:
            try:
                uuid.UUID(str(extra))
            except (ValueError, AttributeError):
                continue
            try:
                record = MenuItem.objects.values(
                    'id', 'name', 'primary_price', 'discount_details'
                ).get(id=extra)
                extras.append(record)
            except MenuItem.DoesNotExist:
                continue
        return extras

    def get_discount_percentage(self, menu_item):
        # Returns the discount magnitude as a non-negative percentage.
        # Source-of-truth precedence: discount_details.discount_percentage,
        # then discount_details.discount_amount, then derive from discounted_price.
        from decimal import Decimal
        if not menu_item.is_discount_active():
            return 0
        primary = Decimal(str(menu_item.primary_price or 0))
        if primary == 0:
            return 0

        details = menu_item.discount_details or {}
        if isinstance(details, dict):
            pct = Decimal(str(details.get('discount_percentage', 0) or 0))
            amt = Decimal(str(details.get('discount_amount', 0) or 0))
            if pct > 0:
                return float(round(pct, 2))
            if amt > 0:
                return float(round((amt / primary) * Decimal('100'), 2))

        if menu_item.discounted_price is not None:
            diff = primary - Decimal(str(menu_item.discounted_price))
            if diff <= 0:
                return 0
            return float(round((diff / primary) * Decimal('100'), 2))
        return 0

    def get_is_discount_active(self, menu_item):
        return menu_item.is_discount_active()

    def get_current_price(self, menu_item):
        # Effective BASE price (no modifiers): discounted when the discount is
        # active, else primary_price. Serialized as a string to match the
        # Decimal money convention of primary_price/discounted_price here.
        from decimal import Decimal
        return str(menu_item.effective_base_price().quantize(Decimal('0.01')))


class SerializerPutTable(ModelSerializer):
    """
    serializer for adding a table
    """
    class Meta:
        model = Table
        fields = '__all__'


class SerializerPublicGetTable(ModelSerializer):
    """
    serializer for getting tables
    """
    dining_area = SerializerMethodField()

    class Meta:
        model = Table
        fields = '__all__'
    
    def get_dining_area(self, table):
        if table.dining_area is None:
            return None
        return {
            'name': table.dining_area.name,
            'available': table.dining_area.available,
            'smoking_zone': table.dining_area.smoking_zone,
            'outdoor_seating': table.dining_area.outdoor_seating,
            'is_indoor': table.dining_area.is_indoor,
            'accessible': table.dining_area.accessible,
            'default_server_section': table.dining_area.default_server_section,
            'is_active': table.dining_area.is_active,
        }


class SerializerMiscPublicTable(ModelSerializer):
    """
    public table listing for the AllowAny misc-public endpoint.

    Diner-safe fields only: identity, capacity and availability plus the
    dining_area summary. Deliberately excludes internal-only Table columns
    (QR tokens, floor-plan coordinates, lifecycle/soft-delete/bookkeeping
    fields, and the restaurant FK).
    """
    dining_area = SerializerMethodField()

    class Meta:
        model = Table
        fields = (
            "id", "number", "str_number", "display_name",
            "min_capacity", "max_capacity", "status", "reserved",
            "dining_area",
        )

    def get_dining_area(self, table):
        if table.dining_area is None:
            return None
        return {
            'name': table.dining_area.name,
            'available': table.dining_area.available,
            'smoking_zone': table.dining_area.smoking_zone,
            'outdoor_seating': table.dining_area.outdoor_seating,
            'is_indoor': table.dining_area.is_indoor,
            'accessible': table.dining_area.accessible,
            'default_server_section': table.dining_area.default_server_section,
            'is_active': table.dining_area.is_active,
        }


class SerializerPublicGetTableDetails(ModelSerializer):
    """
    serializer for getting details of a single table
    """
    current_order = SerializerMethodField()
    restaurant = SerializerMethodField()
    dining_area = SerializerMethodField()
    available = SerializerMethodField()

    class Meta:
        model = Table
        fields = (
            'id', 'number', 'room_name', 'prepayment_required',
            'available', 'current_order', 'restaurant', 'reserved',
            'dining_area', 'enabled',
            'display_name', 'min_capacity', 'max_capacity', 'shape',
            'status', 'tags', 'has_qr', 'qr_mode', 'qr_regenerated_at',
            'floor_x', 'floor_y', 'floor_width', 'floor_height', 'is_active',
        )

    def get_dining_area(self, table):
        if table.dining_area is None:
            return None
        return {
            'name': table.dining_area.name,
            'available': table.dining_area.available,
            'smoking_zone': table.dining_area.smoking_zone,
            'outdoor_seating': table.dining_area.outdoor_seating,
            'is_indoor': table.dining_area.is_indoor,
            'accessible': table.dining_area.accessible,
            'default_server_section': table.dining_area.default_server_section,
            'is_active': table.dining_area.is_active,
        }

    def get_current_order(self, table):
        # Single source of truth for table occupancy: the same fulfilment-axis
        # gate the kitchen board and the order-create path use (not deleted, not
        # cancelled, fulfilment_status != 'served'). The old
        # order_status/payment_status check wrongly flagged a served-but-unpaid
        # order as ongoing — diner payment is unwired, so it stays 'pending' —
        # which blocked the diner while the kitchen board (keyed off the
        # fulfilment axis) showed nothing. Local import avoids a
        # serializers <-> controllers import cycle.
        from orders_app.controllers.con_orders import ConOrder
        result = ConOrder.any_present_ongoing_order(table)
        return {
            'ongoing': result.get('present', False),
            'order_id': result.get('order_id'),
        }

    def get_restaurant(self, table):
        restaurant = table.restaurant
        logo = restaurant.logo
        cover_photo = restaurant.cover_photo

        if logo is not None:
            if len(str(logo)) < 1:
                logo = None
            else:
                logo = str(logo)
        if cover_photo is not None:
            if len(str(cover_photo)) < 1:
                cover_photo = None
            else:
                cover_photo = str(cover_photo)
        return {
            'id': str(restaurant.pk),
            'name': restaurant.name,
            'logo': logo,
            'cover_photo': cover_photo,
            'branding_configuration': restaurant.branding_configuration,
            'socials': restaurant.socials,
            'menu_approval_status': restaurant.first_time_menu_approval_decision,
            'preset_tags': SerializerRestaurantTag(
                RestaurantTag.objects.filter(
                    restaurant=restaurant, deleted=False,
                ).order_by('display_order', 'name'),
                many=True,
            ).data
        }

    def get_available(self, table):
        # Reuse the already-loaded instance — avoids a redundant Table re-fetch
        # inside get_table_availability.
        return get_table_availability(table=table)


class SerializerGetFullMenu(ModelSerializer):
    item_count = SerializerMethodField()
    groups = SerializerMethodField()
    items = SerializerMethodField()
    is_currently_active = SerializerMethodField()

    class Meta:
        model = MenuSection
        fields = (
            'id', 'name', 'section_banner_image', 'available',
            'availability', 'schedules', 'is_currently_active',
            'item_count', 'groups', 'items'
        )

    def get_is_currently_active(self, section):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active
        )
        return is_section_currently_active(section)

    def get_groups(self, section):
        filters = {
            'section': section,
            'approved': True,
            'enabled': True
        }
        if self.context.get('ignore_approval') == 'true':
            filters.pop('approved')
            filters.pop('enabled')
        groups = SectionGroup.objects.filter(**filters)
        return [
            {
                'id': str(group.pk),
                'name': str(group.name)
            } for group in groups
        ]

    def get_items(self, section):
        filters = {
            'section': section,
            'approved': True,
            'enabled': True,
            # 'section_group__deleted': False,
            # 'section_group__available': True,
            'deleted': False,
            'available': True
        }
        if self.context.get('ignore_approval') in ['true', True]:
            filters.pop('approved')
            filters.pop('enabled')
        items = MenuItem.objects.filter(**filters)
        return SerializerPublicGetMenuItem(
            items, many=True
        ).data

    def get_item_count(self, section):
        filters = {
            'section': section,
            'approved': True,
            'enabled': True,
            # 'section_group__deleted': False,
            # 'section_group__available': True,
            'deleted': False,
            'available': True
        }
        if self.context.get('ignore_approval') in ['true', True]:
            filters.pop('approved')
            filters.pop('enabled')
        return MenuItem.objects.filter(**filters).count()


class SerializerPutDiningArea(ModelSerializer):
    class Meta:
        model = DiningArea
        fields = '__all__'


class SerializerGetDiningArea(ModelSerializer):
    no_tables = SerializerMethodField()
    tables = SerializerMethodField()

    class Meta:
        model = DiningArea
        fields = (
            'id', 'name', 'description',
            'smoking_zone', 'outdoor_seating',
            'is_indoor', 'accessible', 'default_server_section', 'is_active',
            'no_tables', 'tables', 'available'
        )

    def get_no_tables(self, dining_area):
        return Table.objects.filter(dining_area=dining_area).count()

    def get_tables(self, dining_area):
        tables = Table.objects.filter(dining_area=dining_area)
        return [
            {
                'id': str(table.pk),
                'number': table.number,
                'available': get_table_availability(table_id=str(table.pk)),
                'reserved': table.reserved,
                'enabled': table.enabled,
                'display_name': table.display_name,
                'min_capacity': table.min_capacity,
                'max_capacity': table.max_capacity,
                'shape': table.shape,
                'status': table.status,
                'tags': table.tags,
                'has_qr': table.has_qr,
                'qr_mode': table.qr_mode,
                'floor_x': table.floor_x,
                'floor_y': table.floor_y,
                'is_active': table.is_active,
            } for table in tables
        ]


class UpsellItemSerializer(ModelSerializer):
    """Serializer for individual upsell items — includes basic menu item info.

    item_price is the ORIGINAL price (primary_price). When item_running_discount
    is true, item_discounted_price holds the effective price; the diner UI shows
    the discounted price with the original struck through and adds the discounted
    price to the basket. Mirrors the discount projection in MenuItemSerializer.
    """
    item_id = serializers.UUIDField(source='menu_item.id', read_only=True)
    item_name = serializers.CharField(source='menu_item.name', read_only=True)
    item_price = serializers.DecimalField(
        source='menu_item.primary_price', max_digits=50, decimal_places=2, read_only=True
    )
    item_discounted_price = serializers.DecimalField(
        source='menu_item.discounted_price', max_digits=50, decimal_places=2,
        read_only=True, allow_null=True
    )
    item_running_discount = serializers.BooleanField(
        source='menu_item.running_discount', read_only=True
    )
    item_discount_percentage = SerializerMethodField()
    item_image = serializers.ImageField(source='menu_item.image', read_only=True)
    item_available = serializers.BooleanField(source='menu_item.available', read_only=True)
    item_in_stock = serializers.BooleanField(source='menu_item.in_stock', read_only=True)

    class Meta:
        model = UpsellItem
        fields = [
            'id', 'menu_item', 'item_id', 'item_name', 'item_price',
            'item_discounted_price', 'item_running_discount', 'item_discount_percentage',
            'item_image', 'item_available', 'item_in_stock', 'listing_position'
        ]

    def get_item_discount_percentage(self, upsell_item):
        # Same precedence as MenuItemSerializer.get_discount_percentage:
        # discount_details.discount_percentage, then discount_amount, then derive
        # from discounted_price. Returns a non-negative percentage (0 when no discount).
        from decimal import Decimal
        menu_item = upsell_item.menu_item
        if not menu_item or not menu_item.running_discount:
            return 0
        primary = Decimal(str(menu_item.primary_price or 0))
        if primary == 0:
            return 0
        details = menu_item.discount_details or {}
        if isinstance(details, dict):
            pct = Decimal(str(details.get('discount_percentage', 0) or 0))
            amt = Decimal(str(details.get('discount_amount', 0) or 0))
            if pct > 0:
                return float(round(pct, 2))
            if amt > 0:
                return float(round((amt / primary) * Decimal('100'), 2))
        if menu_item.discounted_price is not None:
            diff = primary - Decimal(str(menu_item.discounted_price))
            if diff <= 0:
                return 0
            return float(round((diff / primary) * Decimal('100'), 2))
        return 0


class UpsellConfigSerializer(ModelSerializer):
    """Full upsell config with nested items."""
    items = UpsellItemSerializer(source='upsell_items', many=True, read_only=True)

    class Meta:
        model = UpsellConfig
        fields = [
            'id', 'enabled', 'title', 'max_items_to_show',
            'hide_if_in_basket', 'hide_out_of_stock', 'items'
        ]


class UpsellConfigUpdateSerializer(ModelSerializer):
    """For updating config settings (without items)."""
    class Meta:
        model = UpsellConfig
        fields = ['enabled', 'title', 'max_items_to_show', 'hide_if_in_basket', 'hide_out_of_stock']


class SerializerPutReservation(ModelSerializer):
    class Meta:
        model = Reservation
        fields = '__all__'


class SerializerGetReservation(ModelSerializer):
    table_info = SerializerMethodField()

    class Meta:
        model = Reservation
        fields = (
            'id', 'restaurant', 'table', 'table_info',
            'guest_name', 'guest_phone', 'guest_email',
            'date_time', 'party_size', 'status',
            'area_preference', 'notes', 'tags',
            'seated_at', 'server',
            'time_created', 'time_last_updated',
        )

    def get_table_info(self, reservation):
        if reservation.table is None:
            return None
        return {
            'id': str(reservation.table.pk),
            'number': reservation.table.number,
            'display_name': reservation.table.display_name,
        }


class SerializerPutWaitlistEntry(ModelSerializer):
    class Meta:
        model = WaitlistEntry
        fields = '__all__'


class SerializerGetWaitlistEntry(ModelSerializer):
    seated_table_info = SerializerMethodField()

    class Meta:
        model = WaitlistEntry
        fields = (
            'id', 'restaurant', 'guest_name', 'guest_phone',
            'party_size', 'quoted_wait_min', 'quoted_wait_max',
            'added_at', 'tags', 'notes', 'status',
            'seated_table', 'seated_table_info', 'seated_at',
            'time_created', 'time_last_updated',
        )

    def get_seated_table_info(self, entry):
        if entry.seated_table is None:
            return None
        return {
            'id': str(entry.seated_table.pk),
            'number': entry.seated_table.number,
            'display_name': entry.seated_table.display_name,
        }
