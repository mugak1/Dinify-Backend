"""
models for the restaurant app
"""
from django.db import models
from users_app.models import BaseModel, User
from dinify_backend.configss.string_definitions import RestaurantStatus_Pending


# Create your models here.
SUBSCRIPTION_CHOICES = (
    ('per_order', 'Per Order'),
    ('monthly', 'Monthly'),
    ('yearly', 'Yearly')
)


class Restaurant(BaseModel):
    """
    the model for the restaurant
    """
    name = models.CharField(max_length=255, db_index=True)
    location = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    logo = models.ImageField(null=True, upload_to='restaurant_logos/')
    cover_photo = models.ImageField(null=True, upload_to='restaurant_cover_photos/')
    status = models.CharField(max_length=255, default=RestaurantStatus_Pending)

    # dynamic configurations
    # if the orders should be prepaid before submission
    require_order_prepayments = models.BooleanField(default=False)
    # if to show ratings and reviews to the public
    expose_order_ratings = models.BooleanField(default=True)
    # if the restaurant allows deliveries
    allow_deliveries = models.BooleanField(default=False)
    # if the restaurant allows pickups
    allow_pickups = models.BooleanField(default=False)

    preferred_subscription_method = models.CharField(
        max_length=255,
        choices=SUBSCRIPTION_CHOICES,
        default='per_order'
    )
    order_surcharge_percentage = models.FloatField(default=1.0)
    flat_fee = models.FloatField(default=0.0)
    order_surcharge_min_amount = models.FloatField(default=0.0)
    order_surcharge_cap_amount = models.FloatField(default=0.0)

    subscription_validity = models.BooleanField(default=True)
    subscription_expiry_date = models.DateTimeField(null=True, blank=True)

    branding_configuration = models.JSONField(default=dict)
    country = models.CharField(max_length=5, default="UG")

    # for batch approvals
    first_time_menu_approval_decision = models.CharField(
        max_length=255,
        default='pending',
    )
    first_time_menu_approval = models.BooleanField(default=False)

    # eod processing
    eod_restaurant_last_date = models.DateField(null=True, db_index=True)
    eod_restaurant_status = models.IntegerField(default=0, db_index=True)

    class Meta:
        """
        the metadata for the Restaurant model
        """
        db_table = 'restaurants'
        ordering = ['name']
        unique_together = ['name', 'location', 'owner']


class RestaurantEmployee(BaseModel):
    """
    the employees at the restaurant i.e. the waiters, chefs, etc
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    roles = models.JSONField(default=list)
    active = models.BooleanField(default=True)

    class Meta:
        """
        the metadata for the RestaurantEmployee model
        """
        db_table = 'restaurant_employees'
        ordering = ['restaurant', 'user__first_name']
        unique_together = ['user', 'restaurant']


class MenuSection(BaseModel):
    """
    the sections of the menu
    """
    name = models.CharField(max_length=255)
    listing_position = models.IntegerField(default=0)
    description = models.TextField(null=True, blank=True)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    section_banner_image = models.ImageField(null=True, blank=True, upload_to='menu_section_banners/')  # noqa

    # if the section is available or not.
    # e.g. breakfast availability may end at noon
    available = models.BooleanField(default=True)

    # for approvals and enabling items
    approved = models.BooleanField(default=False)
    enabled = models.BooleanField(default=False)

    class Meta:
        """
        the metadata for the MenuSection model
        """
        db_table = 'menu_sections'
        ordering = ['listing_position', 'time_created']
        unique_together = ['name', 'restaurant']


class SectionGroup(BaseModel):
    """
    the groups for a section
    """
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    section = models.ForeignKey(MenuSection, on_delete=models.CASCADE)
    available = models.BooleanField(default=True)

    # for approvals and enabling items
    approved = models.BooleanField(default=False)
    enabled = models.BooleanField(default=False)

    class Meta:
        """
        the metadata for the SectionGroup model
        """
        db_table = 'section_groups'
        ordering = ['name']
        unique_together = ['name', 'section']


class MenuItem(BaseModel):
    """
    the items in the menu
    """
    section = models.ForeignKey(MenuSection, on_delete=models.CASCADE)
    section_group = models.ForeignKey(SectionGroup, on_delete=models.CASCADE, null=True, blank=True)
    image = models.ImageField(null=True, blank=True, upload_to='menu_items/')

    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    allergens = models.JSONField(default=list)
    primary_price = models.FloatField()

    discounted_price = models.FloatField(null=True, blank=True)
    running_discount = models.BooleanField(default=False)
    consider_discount_object = models.BooleanField(default=False)
    discount_description = models.TextField(null=True, blank=True)
    discount_details = models.JSONField(default=dict)
    # e.g. {
    #     recurring_days: [1-7] monday is 1, sunday is 7,
    #     start_date: '',
    #     end_date: '',
    #     start_time: '',
    #     end_time: '',
    #     discount_percentage: 0.0,
    #     discount_amount: 0.0
    # }

    # if the kitchen can process the item or not
    available = models.BooleanField(default=True)
    is_extra = models.BooleanField(default=False)

    options = models.JSONField(default=dict)
    # e.g. {
    # min_selections: 0,
    # max_selections: 0,
    # options : [{
    #   name: '',
    #   selectable: boolean, i.e. does it have options to select from
    #   choices: [Spicy, Not spicy, Extra spicy],
    #   cost: 0
    # }, {...}, {...}]
    has_extras = models.BooleanField(default=False)
    extras_applicable = models.JSONField(default=list)

    # for approvals and enabling items
    approved = models.BooleanField(default=False)
    enabled = models.BooleanField(default=False)

    class Meta:
        """
        the metadata for the MenuItem model
        """
        db_table = 'menu_items'
        ordering = ['section', 'name']
        unique_together = ['name', 'section']


class DiningArea(BaseModel):
    """
    the dining areas at the restaurant
    """
    name = models.CharField(max_length=255)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    description = models.TextField(null=True, blank=True)
    available = models.BooleanField(default=True)
    smoking_zone = models.BooleanField(default=False)
    outdoor_seating = models.BooleanField(default=False)

    class Meta:
        """
        the metadata for the DiningArea model
        """
        db_table = 'dining_areas'
        ordering = ['name']
        unique_together = ['name', 'restaurant']


class Table(BaseModel):
    """
    the tables at the restaurant
    """
    number = models.IntegerField()
    str_number = models.CharField(max_length=20, blank=True, default='')
    prepayment_required = models.BooleanField(default=False)

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    dining_area = models.ForeignKey(DiningArea, on_delete=models.SET_NULL, null=True, blank=True)

    # === deprecated
    room_name = models.CharField(max_length=255, null=True, blank=True)
    smoking_zone = models.BooleanField(default=False)
    outdoor_seating = models.BooleanField(default=False)
    # === end deprecated

    # if the table is available for use or not
    # available = models.BooleanField(default=True)
    reserved = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)

    class Meta:
        """
        the metadata for the Table model
        """
        db_table = 'tables'
        ordering = ['number']
        unique_together = ['number', 'str_number', 'restaurant']
