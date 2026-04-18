"""
models for the restaurant app
"""
from decimal import Decimal

from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from users_app.models import BaseModel, User
from dinify_backend.configss.string_definitions import RestaurantStatus_Pending
from rest_framework.serializers import ModelSerializer
from misc_app.controllers.utils.archive_record import archive_record


# Create your models here.
SUBSCRIPTION_CHOICES = (
    ('per_order', 'Per Order'),
    ('monthly', 'Monthly'),
    ('yearly', 'Yearly')
)


def default_branding_configuration():
    return {
        'home': {
            'bgColor': '#f5f5f5',
            'headerCase': 'none',
            'headerShow': 'name',
            'headerColor': '#171717',
            'headerTextColor': '#ffffff',
            'headerShowName': '',
            'viewMenuBgColor': '#dc2626',
            'headerFontWeight': '600',
            'viewMenuTextColor': '#ffffff'
        }
    }


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
    order_surcharge_percentage = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('1.0'))
    flat_fee = models.DecimalField(max_digits=50, decimal_places=2, default=Decimal('0.00'))
    order_surcharge_min_amount = models.DecimalField(max_digits=50, decimal_places=2, default=Decimal('0.00'))
    order_surcharge_cap_amount = models.DecimalField(max_digits=50, decimal_places=2, default=Decimal('0.00'))

    subscription_validity = models.BooleanField(default=True)
    subscription_expiry_date = models.DateTimeField(null=True, blank=True)

    branding_configuration = models.JSONField(default=default_branding_configuration)
    preset_tags = models.JSONField(default=list, blank=True)
    country = models.CharField(max_length=5, default="UG")

    # for batch approvals
    first_time_menu_approval_decision = models.CharField(
        max_length=255,
        default='approve',
    )
    first_time_menu_approval = models.BooleanField(default=True)

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

    def save(self, *args, **kwargs):
        for field_name in ('logo', 'cover_photo'):
            field = getattr(self, field_name)
            if not field:
                continue
            optimize_new = False
            if self.pk:
                try:
                    old = Restaurant.objects.get(pk=self.pk)
                    if getattr(old, field_name) != field:
                        optimize_new = True
                except Restaurant.DoesNotExist:
                    optimize_new = True
            else:
                optimize_new = True
            if optimize_new:
                from restaurants_app.utils.image_optimizer import optimize_image
                optimize_image(field)
        super().save(*args, **kwargs)


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

    availability = models.CharField(
        max_length=20,
        default='always',
        choices=[('always', 'Always'), ('scheduled', 'Scheduled')]
    )
    # Controls whether this section is always visible or only during scheduled hours.

    schedules = models.JSONField(default=list, blank=True)
    # List of schedule slots when availability == 'scheduled':
    # [
    #     {
    #         "id": "uuid",
    #         "days": [1, 2, 3, 4, 5],       # 1=Monday, 7=Sunday
    #         "startTime": "07:00",            # 24-hour format
    #         "endTime": "11:00"               # 24-hour format
    #     }
    # ]

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

    def save(self, *args, **kwargs):
        if self.section_banner_image:
            optimize_new = False
            if self.pk:
                try:
                    old = MenuSection.objects.get(pk=self.pk)
                    if old.section_banner_image != self.section_banner_image:
                        optimize_new = True
                except MenuSection.DoesNotExist:
                    optimize_new = True
            else:
                optimize_new = True
            if optimize_new:
                from restaurants_app.utils.image_optimizer import optimize_image
                optimize_image(self.section_banner_image, max_width=1200, max_height=400)
        super().save(*args, **kwargs)


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
    calories = models.PositiveIntegerField(null=True, blank=True, help_text="Calorie count (kcal)")
    allergens = models.JSONField(default=list)
    tags = models.JSONField(default=list, blank=True)
    primary_price = models.DecimalField(max_digits=50, decimal_places=2)

    discounted_price = models.DecimalField(max_digits=50, decimal_places=2, null=True, blank=True)
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
    in_stock = models.BooleanField(default=True)
    is_extra = models.BooleanField(default=False)
    is_special = models.BooleanField(default=False)
    is_featured = models.BooleanField(default=False)
    is_popular = models.BooleanField(default=False)
    is_new = models.BooleanField(default=False)

    options = models.JSONField(default=dict)
    # Grouped modifier structure (new):
    # {
    #     "hasModifiers": bool,
    #     "groups": [
    #         {
    #             "id": "uuid",
    #             "name": "Choose your size",
    #             "required": bool,
    #             "selectionType": "single" | "multi",
    #             "minSelections": int,
    #             "maxSelections": int,
    #             "choices": [
    #                 { "id": "uuid", "name": "Small", "additionalCost": 0, "available": bool }
    #             ]
    #         }
    #     ]
    # }
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

    def save(self, *args, **kwargs):
        optimize_new_image = False
        if self.pk:
            try:
                old_instance = MenuItem.objects.get(pk=self.pk)
                if old_instance.image != self.image:
                    optimize_new_image = True
            except MenuItem.DoesNotExist:
                optimize_new_image = bool(self.image)
        else:
            optimize_new_image = bool(self.image)

        if optimize_new_image and self.image:
            from restaurants_app.utils.image_optimizer import optimize_image
            optimize_image(self.image)

        super().save(*args, **kwargs)


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
    is_indoor = models.BooleanField(default=True)
    accessible = models.BooleanField(default=False)
    default_server_section = models.CharField(max_length=10, blank=True, default='')
    is_active = models.BooleanField(default=True)

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

    display_name = models.CharField(max_length=100, blank=True, default='')
    min_capacity = models.IntegerField(default=1)
    max_capacity = models.IntegerField(default=4)
    shape = models.CharField(
        max_length=20,
        default='square',
        choices=[
            ('round', 'Round'),
            ('square', 'Square'),
            ('rectangle', 'Rectangle'),
            ('bar', 'Bar'),
        ]
    )
    status = models.CharField(
        max_length=20,
        default='available',
        choices=[
            ('available', 'Available'),
            ('seated', 'Seated'),
            ('bill_requested', 'Bill Requested'),
            ('dirty', 'Dirty'),
            ('out_of_service', 'Out of Service'),
        ]
    )
    tags = models.JSONField(default=list, blank=True)
    has_qr = models.BooleanField(default=False)
    qr_mode = models.CharField(
        max_length=20,
        default='order_pay',
        choices=[
            ('menu_only', 'Menu Only'),
            ('order_pay', 'Order & Pay'),
            ('order_only', 'Order Only'),
        ]
    )
    qr_regenerated_at = models.DateTimeField(null=True, blank=True)
    floor_x = models.FloatField(default=50.0)
    floor_y = models.FloatField(default=50.0)
    floor_width = models.FloatField(default=10.0)
    floor_height = models.FloatField(default=10.0)
    is_active = models.BooleanField(default=True)

    class Meta:
        """
        the metadata for the Table model
        """
        db_table = 'tables'
        ordering = ['number']
        unique_together = ['number', 'str_number', 'restaurant']


class Reservation(BaseModel):
    """
    Guest booking / reservation for a restaurant table.
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True)
    guest_name = models.CharField(max_length=255)
    guest_phone = models.CharField(max_length=50, blank=True, default='')
    guest_email = models.CharField(max_length=255, blank=True, default='')
    date_time = models.DateTimeField()
    party_size = models.IntegerField(default=2)
    status = models.CharField(
        max_length=20,
        default='confirmed',
        choices=[
            ('confirmed', 'Confirmed'),
            ('arrived', 'Arrived'),
            ('late', 'Late'),
            ('no_show', 'No Show'),
            ('seated', 'Seated'),
            ('cancelled', 'Cancelled'),
        ]
    )
    area_preference = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    tags = models.JSONField(default=dict, blank=True)
    seated_at = models.DateTimeField(null=True, blank=True)
    server = models.ForeignKey(
        RestaurantEmployee, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        db_table = 'reservations'
        ordering = ['date_time']


class WaitlistEntry(BaseModel):
    """
    Walk-in queue entry for a restaurant.
    """
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE)
    guest_name = models.CharField(max_length=255)
    guest_phone = models.CharField(max_length=50, blank=True, default='')
    party_size = models.IntegerField(default=2)
    quoted_wait_min = models.IntegerField(default=15)
    quoted_wait_max = models.IntegerField(default=30)
    added_at = models.DateTimeField(auto_now_add=True)
    tags = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default='')
    status = models.CharField(
        max_length=20,
        default='waiting',
        choices=[
            ('waiting', 'Waiting'),
            ('seated', 'Seated'),
            ('left', 'Left'),
        ]
    )
    seated_table = models.ForeignKey(
        Table, on_delete=models.SET_NULL, null=True, blank=True
    )
    seated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'waitlist_entries'
        ordering = ['added_at']


class UpsellConfig(BaseModel):
    """
    Per-restaurant configuration for checkout upsell suggestions.
    One config per restaurant (created on first access).
    """
    restaurant = models.OneToOneField(
        Restaurant,
        on_delete=models.CASCADE,
        related_name='upsell_config'
    )
    enabled = models.BooleanField(default=False)
    title = models.CharField(max_length=255, default="You might also like")
    max_items_to_show = models.IntegerField(default=6)
    hide_if_in_basket = models.BooleanField(default=True)
    hide_out_of_stock = models.BooleanField(default=True)

    class Meta:
        db_table = 'upsell_configs'


class UpsellItem(BaseModel):
    """
    A menu item included in the upsell carousel.
    Ordering is controlled by listing_position.
    """
    config = models.ForeignKey(
        UpsellConfig,
        on_delete=models.CASCADE,
        related_name='upsell_items'
    )
    menu_item = models.ForeignKey(
        MenuItem,
        on_delete=models.CASCADE,
        related_name='upsell_entries'
    )
    listing_position = models.IntegerField(default=0)

    class Meta:
        db_table = 'upsell_items'
        ordering = ['listing_position']
        unique_together = ['config', 'menu_item']


# serializers for archival
# serializers used in the same file to avoid circular dependencies
class SerArcRestaurant(ModelSerializer):
    class Meta:
        model = Restaurant
        fields = '__all__'


@receiver(post_save, sender=Restaurant)
def archive_restaurant(sender, instance, **kwargs):
    record_data = SerArcRestaurant(instance).data
    archive_record(record_data, 'archive_restaurants')


class SerArcMenuSection(ModelSerializer):
    class Meta:
        model = MenuSection
        fields = '__all__'


@receiver(post_save, sender=MenuSection)
def archive_menu_section(sender, instance, **kwargs):
    record_data = SerArcMenuSection(instance).data
    archive_record(record_data, 'archive_menu_sections')


class SerArcSectionGroup(ModelSerializer):
    class Meta:
        model = SectionGroup
        fields = '__all__'

@receiver(post_save, sender=SectionGroup)
def archive_section_group(sender, instance, **kwargs):
    record_data = SerArcSectionGroup(instance).data
    archive_record(record_data, 'archive_section_groups')


class SerArcMenuItem(ModelSerializer):
    class Meta:
        model = MenuItem
        fields = '__all__'


@receiver(post_save, sender=MenuItem)
def archive_menu_item(sender, instance, **kwargs):
    record_data = SerArcMenuItem(instance).data
    archive_record(record_data, 'archive_menu_items')


class SerArcDiningArea(ModelSerializer):
    class Meta:
        model = DiningArea
        fields = '__all__'


@receiver(post_save, sender=DiningArea)
def archive_dining_area(sender, instance, **kwargs):
    record_data = SerArcDiningArea(instance).data
    archive_record(record_data, 'archive_dining_areas')


class SerArcTable(ModelSerializer):
    class Meta:
        model = Table
        fields = '__all__'


@receiver(post_save, sender=Table)
def archive_table(sender, instance, **kwargs):
    record_data = SerArcTable(instance).data
    archive_record(record_data, 'archive_tables')


class SerArcRestaurantEmployee(ModelSerializer):
    class Meta:
        model = RestaurantEmployee
        fields = '__all__'


@receiver(post_save, sender=RestaurantEmployee)
def archive_restaurant_employee(sender, instance, **kwargs):
    record_data = SerArcRestaurantEmployee(instance).data
    archive_record(record_data, 'archive_restaurant_employees')


class SerArcUpsellConfig(ModelSerializer):
    class Meta:
        model = UpsellConfig
        fields = '__all__'


@receiver(post_save, sender=UpsellConfig)
def archive_upsell_config(sender, instance, **kwargs):
    record_data = SerArcUpsellConfig(instance).data
    archive_record(record_data, 'archive_upsell_configs')


class SerArcUpsellItem(ModelSerializer):
    class Meta:
        model = UpsellItem
        fields = '__all__'


@receiver(post_save, sender=UpsellItem)
def archive_upsell_item(sender, instance, **kwargs):
    record_data = SerArcUpsellItem(instance).data
    archive_record(record_data, 'archive_upsell_items')


class SerArcReservation(ModelSerializer):
    class Meta:
        model = Reservation
        fields = '__all__'


@receiver(post_save, sender=Reservation)
def archive_reservation(sender, instance, **kwargs):
    record_data = SerArcReservation(instance).data
    archive_record(record_data, 'archive_reservations')


class SerArcWaitlistEntry(ModelSerializer):
    class Meta:
        model = WaitlistEntry
        fields = '__all__'


@receiver(post_save, sender=WaitlistEntry)
def archive_waitlist_entry(sender, instance, **kwargs):
    record_data = SerArcWaitlistEntry(instance).data
    archive_record(record_data, 'archive_waitlist_entries')


