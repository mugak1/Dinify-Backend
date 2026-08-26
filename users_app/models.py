"""
the models for the Users app
"""
import uuid
import datetime
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.utils import timezone
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_CHOICES,
    ACCOUNT_TYPE_RESTAURANT_USER,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_STATE_CHOICES,
    CUSTOMER_ACCESS_STATES,
)


# Create your models here.
class User(AbstractUser):
    """
    the user/auth model for dinify
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    # personal information
    country = models.CharField(max_length=255, null=True, blank=True)
    first_name = models.CharField(max_length=255, null=True, blank=True)
    last_name = models.CharField(max_length=255, null=True, blank=True)
    other_names = models.CharField(max_length=255, null=True, blank=True)

    # contact/auth information
    # either of these will be used for login
    # but primarily the phone number shall be considered
    email = models.EmailField(max_length=255, db_index=True, null=True, blank=True)
    # Nullable ONLY so a platform-staff account can exist without one: the admin
    # plane authenticates by password + TOTP and must never need a phone or SMS.
    # It stays REQUIRED for restaurant users — enforced at the write sites
    # (REQUIRED_INFORMATION['new_user'] via self_register), not by the column. Postgres treats NULLs as distinct under a UNIQUE
    # constraint, so uniqueness for real numbers is unaffected.
    # NOTE: null=True WITHOUT blank=True is deliberate. `blank=True` would make
    # every ModelSerializer treat the field as required=False, silently loosening
    # customer registration; leaving blank=False keeps DRF's required=True.
    phone_number = models.CharField(
        max_length=255, unique=True, db_index=True, null=True,
    )

    roles = models.JSONField(default=list)
    prompt_password_change = models.BooleanField(default=True)

    # platform-admin identity discriminator (restaurant_user | platform_staff).
    # Inert until the admin control-plane PRs consume it; never Secretary-editable
    # and never exposed writable on a serializer.
    account_type = models.CharField(
        max_length=32,
        choices=ACCOUNT_TYPE_CHOICES,
        default=ACCOUNT_TYPE_RESTAURANT_USER,
        db_index=True,
    )

    # CUSTOMER-PLANE ACCESS STATE (Step 2D.1). Whether this identity may be admitted
    # onto the customer plane AT ALL — a separate axis from every neighbouring field:
    #
    #   account_type   which plane the account belongs to
    #   is_active      whether it was administratively deactivated
    #   password       whether one particular credential authenticates
    #   this field     whether the identity is admitted to the plane in the first place
    #
    # `established` is the default and covers every identity that predates this gate
    # plus every ordinary creation path (self-registration, staff invite, the
    # order-matching command). `pending_initial_claim` is written by exactly one
    # caller — `platform_admin_app.onboarding_creation` creating a BRAND-NEW owner —
    # and is enforced at every customer token mint, at token presentation and at
    # refresh (`users_app.customer_access`).
    #
    # NOT owner-control evidence. Owner control stays restaurant-scoped and
    # evidence-based (a consumed OwnerInvitation for the current owner); this is
    # identity-scoped operational authorization, and the two must not be conflated —
    # an established owner of restaurant A who is named owner of a new restaurant B
    # keeps full customer access while B's invitation is still pending.
    #
    # SERVER-WRITTEN ONLY, and protected the same way `account_type` is: absent from
    # `SerGetUserProfile.fields`, absent from every EDIT_INFORMATION section (there is
    # no `user` section at all), and never assigned by a customer-plane controller.
    #
    # `db_default` as well as `default` is deliberate — see migration 0014.
    customer_access_state = models.CharField(
        max_length=32,
        choices=CUSTOMER_ACCESS_STATE_CHOICES,
        default=CUSTOMER_ACCESS_ESTABLISHED,
        db_default=CUSTOMER_ACCESS_ESTABLISHED,
    )

    # track if profile is

    class Meta:
        """
        the metadata for the User model
        """
        db_table = 'users'
        ordering = ['username']
        constraints = [
            # The vocabulary as a DATABASE fact, matching how the other closed
            # vocabularies in this repo are held (`Restaurant.status`,
            # `RestaurantOnboarding.source`). `choices=` is a form/admin nicety and
            # not an integrity boundary: a value outside this set would be read by
            # `customer_access.is_established` as "not established" and would silently
            # lock the account out, so it must not be storable in the first place.
            models.CheckConstraint(
                condition=models.Q(customer_access_state__in=CUSTOMER_ACCESS_STATES),
                name='user_customer_access_state_vocabulary',
            ),
        ]


class BaseModel(models.Model):
    """
    The base model for the models in the application
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    time_created = models.DateTimeField(auto_now_add=True)
    time_last_updated = models.DateTimeField(auto_now=True)
    time_deleted = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='%(class)s_created_by'
    )

    deleted = models.BooleanField(default=False)
    deletion_reason = models.CharField(max_length=255, null=True, blank=True)
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='%(class)s_deleted_by'
    )

    # determine if the record has been archived
    archived = models.BooleanField(default=False)
    vacuumed = models.BooleanField(default=False)

    # eod_processing
    eod_last_date = models.DateField(null=True, db_index=True)
    eod_record_date = models.DateField(null=True, db_index=True)

    class Meta:
        """
        the metadata for the BaseModel model
        """
        abstract = True


class UserOtp(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    msisdn = models.CharField(max_length=255, null=True, blank=True)
    purpose = models.CharField(max_length=255, null=True, blank=True)
    otp_hash = models.CharField(max_length=255)
    salt = models.CharField(max_length=64, default='')
    identifier = models.CharField(max_length=255, default='', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    time_created = models.DateTimeField(auto_now_add=True)
    expiry_time = models.DateTimeField()

    class Meta:
        db_table = 'user_otps'
        ordering = ['time_created']


@receiver(pre_save, sender=UserOtp)
def set_expiry_time(sender, instance, **kwargs):
    instance.expiry_time = timezone.now() + datetime.timedelta(minutes=5)
