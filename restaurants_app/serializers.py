"""
the serializers for the restaurant app
"""
import json
import logging
import uuid

logger = logging.getLogger(__name__)

from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order, OrderItem
from rest_framework import serializers
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table,
    SectionGroup, DiningArea, UpsellConfig, UpsellItem,
    Reservation, WaitlistEntry
)
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Initiated,
    OrderItemStatus_Preparing,
    OrderStatus_Pending,
    OrderStatus_Served,
    PaymentStatus_Paid
)
from finance_app.serializers import SerializerPutAccount
from finance_app.models import DinifyAccount
from restaurants_app.controllers.tables import get_table_availability


class SerializerGetRestaurantDetail(ModelSerializer):
    account = SerializerMethodField()

    class Meta:
        """
        the meta class for the serializers
        """
        model = Restaurant
        fields = '__all__'

    def get_account(self, restaurant):
        try:
            account = DinifyAccount.objects.get(restaurant=restaurant)
            return SerializerPutAccount(account, many=False).data
        except DinifyAccount.DoesNotExist:
            return {}
        except Exception as error:
            logger.error("Error in getting account: %s", error)
            return None


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
    class Meta:
        model = MenuItem
        fields = '__all__'


class SerializerPublicGetMenuItem(ModelSerializer):
    """
    serializer for getting the menu Item
    """
    has_options = SerializerMethodField()
    group = SerializerMethodField()
    extras = SerializerMethodField()
    discount_percentage = SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = (
            'id', 'name', 'description', 'calories', 'primary_price',
            'discounted_price', 'running_discount', 'image',
            'available', 'in_stock', 'allergens', 'tags', 'discount_details',
            'has_options', 'options', 'section', 'group', 'extras', 'is_extra',
            'discount_percentage', 'has_extras', 'is_special',
            'is_featured', 'is_popular', 'is_new'
        )

    def get_has_options(self, menu_item):
        options = menu_item.options
        if not options:
            return False
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (ValueError, TypeError):
                return False
        if not isinstance(options, dict):
            return False
        # New grouped format
        if 'hasModifiers' in options:
            return options.get('hasModifiers', False)
        # Legacy flat format
        if 'options' in options:
            return len(options.get('options', [])) > 0
        return False

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
                    'id', 'name', 'primary_price'
                ).get(id=extra)
                extras.append(record)
            except MenuItem.DoesNotExist:
                continue
        return extras

    def get_discount_percentage(self, menu_item):
        from decimal import Decimal
        if not menu_item.running_discount:
            return 0
        if menu_item.discounted_price is None:
            return 0
        difference = menu_item.discounted_price - menu_item.primary_price
        if difference == 0:
            return 0
        percentage = (difference / menu_item.primary_price) * Decimal('100')
        return float(round(percentage, 2))


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
        orders = Order.objects.values('id').filter(
            table=table,
            order_status__in=[
                OrderItemStatus_Initiated,
                OrderStatus_Pending,
                OrderItemStatus_Preparing,
                OrderStatus_Served
            ]
        ).exclude(
            payment_status=PaymentStatus_Paid
        ).order_by('-time_created')

        present = orders.count() > 0
        order_id = None

        if present:
            order_id = orders.first()['id']

        return {
            'ongoing': present,
            'order_id': order_id
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
            'menu_approval_status': restaurant.first_time_menu_approval_decision,
            'preset_tags': restaurant.preset_tags or []
        }

    def get_available(self, table):
        return get_table_availability(table_id=str(table.pk))
        # return {
        #     'available': True,
        #     'message': 'Table is available'
        # }


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


class SerializerAdminGetOrderReview(ModelSerializer):
    customer = SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'rating', 'review',
            'block_review', 'customer',
            'order_number', 'time_created'
        )

    def get_customer(self, order):
        if order.customer is None:
            return ''
        return f'{order.customer.first_name}'


class SerializerAdminGetOrderItemReview(ModelSerializer):
    customer = SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'order', 'rating', 'review',
            'block_review', 'customer'
        )

    def get_customer(self, order_item):
        if order_item.order is None or order_item.order.customer is None:
            return ''
        return f'{order_item.order.customer.first_name}'


class SerializerPublicGetOrderReview(ModelSerializer):
    customer = SerializerMethodField()

    class Meta:
        model = Order
        fields = ('rating', 'review', 'customer', 'time_created', 'order_number')

    def get_customer(self, order):
        if order.customer is None:
            return 'Anonymous'
        return f'{order.customer.first_name}'


class SerializerPublicGetOrderItemReview(ModelSerializer):
    customer = SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ('rating', 'review', 'customer')

    def get_customer(self, order_item):
        if order_item.order is None or order_item.order.customer is None:
            return ''
        return f'{order_item.order.customer.first_name}'


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
    """Serializer for individual upsell items — includes basic menu item info."""
    item_id = serializers.UUIDField(source='menu_item.id', read_only=True)
    item_name = serializers.CharField(source='menu_item.name', read_only=True)
    item_price = serializers.DecimalField(
        source='menu_item.primary_price', max_digits=50, decimal_places=2, read_only=True
    )
    item_image = serializers.ImageField(source='menu_item.image', read_only=True)
    item_available = serializers.BooleanField(source='menu_item.available', read_only=True)

    class Meta:
        model = UpsellItem
        fields = [
            'id', 'menu_item', 'item_id', 'item_name', 'item_price',
            'item_image', 'item_available', 'listing_position'
        ]


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
