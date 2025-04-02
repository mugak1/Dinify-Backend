"""
the serializers for the restaurant app
"""
from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order, OrderItem
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table,
    SectionGroup, DiningArea
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
            print(f"Error in getting account: {error}")
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
            "logo", "cover_photo", "status", "owner"
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
            'available', 'item_count', 'has_groups', 'groups',
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
        return SectionGroup.objects.filter(section=menu_section).count() > 0

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

    class Meta:
        model = MenuItem
        fields = (
            'id', 'name', 'description', 'primary_price',
            'discounted_price', 'running_discount', 'image',
            'available', 'allergens', 'discount_details',
            'has_options', 'options', 'group', 'extras', 'is_extra'
        )

    def get_has_options(self, menu_item):
        return len(menu_item.options) > 0

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
        extras = []
        for extra in applicable_extras:
            record = MenuItem.objects.values(
                'id', 'name', 'primary_price'
            ).get(id=extra)
            extras.append(record)
        return extras


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
            'outdoor_seating': table.dining_area.outdoor_seating
        }


class SerializerPublicGetTableDetails(ModelSerializer):
    """
    serializer for getting details of a single table
    """
    current_order = SerializerMethodField()
    restaurant = SerializerMethodField()
    dining_area = SerializerMethodField()

    class Meta:
        model = Table
        fields = (
            'id', 'number', 'room_name', 'prepayment_required',
            'available', 'current_order', 'restaurant', 'reserved',
            'dining_area'
        )

    def get_dining_area(self, table):
        if table.dining_area is None:
            return None
        return {
            'name': table.dining_area.name,
            'available': table.dining_area.available,
            'smoking_zone': table.dining_area.smoking_zone,
            'outdoor_seating': table.dining_area.outdoor_seating
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
            'menu_approval_status': restaurant.first_time_menu_approval_decision
        }


class SerializerGetFullMenu(ModelSerializer):
    item_count = SerializerMethodField()
    groups = SerializerMethodField()
    items = SerializerMethodField()

    class Meta:
        model = MenuSection
        fields = (
            'id', 'name', 'section_banner_image', 'available',
            'item_count', 'groups', 'items'
        )

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
            'section_group__deleted': False,
            'section_group__available': True,
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
            'section_group__deleted': False,
            'section_group__available': True,
            'deleted': False,
            'available': True
        }
        if self.context.get('ignore_approval') == 'true':
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

    def get_customer(self, order):
        if order.customer is None:
            return ''
        return f'{order.customer.first_name}'


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

    def get_customer(self, order):
        if order.customer is None:
            return ''
        return f'{order.customer.first_name}'


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
                'available': table.available,
                'reserved': table.reserved
            } for table in tables
        ]
