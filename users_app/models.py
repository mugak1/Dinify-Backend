"""
the models for the Users app
"""
import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser


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
    prompt_password_change = models.BooleanField(default=False)

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

    class Meta:
        """
        the metadata for the BaseModel model
        """
        abstract = True
