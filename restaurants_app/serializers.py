"""
the serializers for the restaurant app
"""
from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order, OrderItem
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table,
    SectionGroup
)
from dinify_backend.configss.string_definitions import (
    OrderItemStatus_Initiated, OrderItemStatus_Preparing, OrderStatus_Pending
)


class SerializerPutRestaurant(ModelSerializer):
    """
    seriailizer for adding and editing restaurant details
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

    class Meta:
        model = RestaurantEmployee
        fields = (
            "id", "time_created", "time_last_updated",
            "name", "roles", "active"
        )

    def get_name(self, employee):
        """
        returns the name of the employee
        """
        return f"{employee.user.first_name} {employee.user.last_name}"


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
            'available', 'item_count', 'has_groups', 'groups'
        )

    def get_item_count(self, menu_section):
        return MenuItem.objects.filter(section=menu_section).count()

    def get_has_groups(self, menu_section):
        return SectionGroup.objects.filter(section=menu_section).count() > 0

    def get_groups(self, menu_section):
        groups = SectionGroup.objects.filter(section=menu_section)
        return [
            {
                'id': str(group.pk),
                'name': group.name
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
        fields = ('id', 'name', 'item_count')

    def get_item_count(self, group):
        return MenuItem.objects.filter(section_group=group).count()


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
            'available',
            'has_options', 'options', 'group', 'extras'
        )

    def get_has_options(self, menu_item):
        return len(menu_item.options) > 0

    def get_group(self, menu_item):
        if menu_item.section_group is None:
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
    class Meta:
        model = Table
        fields = '__all__'


class SerializerPublicGetTableDetails(ModelSerializer):
    """
    serializer for getting details of a single table
    """
    current_order = SerializerMethodField()
    restaurant = SerializerMethodField()

    class Meta:
        model = Table
        fields = (
            'id', 'number', 'room_name', 'prepayment_required',
            'available', 'current_order', 'restaurant', 'reserved'
        )

    def get_current_order(self, table):
        orders = Order.objects.values('id').filter(
            table=table,
            order_status__in=[
                OrderItemStatus_Initiated,
                OrderStatus_Pending,
                OrderItemStatus_Preparing
            ]
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
            'branding_configuration': restaurant.branding_configuration
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
        groups = SectionGroup.objects.filter(
            section=section,
            approved=True,
            enabled=True
        )
        return [
            {
                'id': str(group.pk),
                'name': str(group.name)
            } for group in groups
        ]

    def get_items(self, section):
        items = MenuItem.objects.filter(
            section=section,
            approved=True,
            enabled=True
        )
        return SerializerPublicGetMenuItem(
            items, many=True
        ).data

    def get_item_count(self, section):
        return MenuItem.objects.filter(
            section=section,
            approved=True,
            enabled=True
        ).count()


class SerializerAdminGetOrderReview(ModelSerializer):
    customer = SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'rating', 'review',
            'block_review', 'customer'
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
        fields = ('rating', 'review', 'customer')

    def get_customer(self, order):
        if order.customer is None:
            return ''
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
