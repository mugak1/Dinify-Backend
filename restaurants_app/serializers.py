"""
the serializers for the restaurant app
"""
from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order
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
    class Meta:
        model = Restaurant
        fields = ("id", "name", "location", "logo", "cover_photo", "status")


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

    class Meta:
        model = MenuSection
        fields = (
            'id', 'name', 'description', 'section_banner_image',
            'available', 'item_count'
        )

    def get_item_count(self, menu_section):
        return MenuItem.objects.filter(section=menu_section).count()


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

    class Meta:
        model = MenuItem
        fields = (
            'id', 'name', 'description', 'primary_price',
            'discounted_price', 'running_discount', 'image',
            'available',
            'has_options', 'options'
        )

    def get_has_options(self, menu_item):
        return len(menu_item.options) > 0


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
            'available', 'current_order', 'restaurant'
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
        if cover_photo is not None:
            if len(str(cover_photo)) < 1:
                cover_photo = None
        return {
            'id': str(restaurant.pk),
            'name': restaurant.name,
            'logo': logo,
            'cover_photo': cover_photo,
            'branding_configuration': restaurant.branding_configuration
        }
