"""
the models for the Users app
"""
import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser


# Create your models here.
class User(AbstractUser):
    """
    the user/auth model for
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
