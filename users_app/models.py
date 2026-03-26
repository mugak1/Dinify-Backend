"""
the models for the Users app
"""
import uuid
import datetime
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone
from rest_framework.serializers import ModelSerializer
from misc_app.controllers.utils.archive_record import archive_record


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
    phone_number = models.CharField(max_length=255, unique=True, db_index=True)

    roles = models.JSONField(default=list)
    prompt_password_change = models.BooleanField(default=True)

    # track if profile is

    class Meta:
        """
        the metadata for the User model
        """
        db_table = 'users'
        ordering = ['username']


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
    time_created = models.DateTimeField(auto_now_add=True)
    expiry_time = models.DateTimeField()

    class Meta:
        db_table = 'user_otps'
        ordering = ['time_created']


@receiver(pre_save, sender=BaseModel)
def add_time_last_updated(sender, instance, **kwargs):
    instance.time_last_updated = timezone.now()


@receiver(pre_save, sender=UserOtp)
def set_expiry_time(sender, instance, **kwargs):
    instance.expiry_time = timezone.now() + datetime.timedelta(minutes=5)


class SerArcUser(ModelSerializer):
    class Meta:
        model = User
        fields = '__all__'


@receiver(post_save, sender=User)
def archive_user(sender, instance, **kwargs):
    data = SerArcUser(instance).data
    data['time_created'] = data['date_joined']
    archive_record(
        record_data=data,
        archive_collection='archive_users',
    )
