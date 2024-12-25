from django.db import models
from users_app.models import User, BaseModel
from restaurants_app.models import Restaurant


# Create your models here.
class ServiceTicket(BaseModel):
    ticket_type = models.CharField(max_length=255)  # options are 'feedback', 'support', 'bug'
    ticket_title = models.CharField(max_length=255)
    ticket_description = models.TextField()
    ticket_status = models.CharField(max_length=255, default='open')
    ticket_priority = models.CharField(max_length=255, default='normal')

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='restaurant_tickets', null=True)  # noqa

    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='ticket_assigned_to', null=True)  # noqa
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='ticket_assigned_by', null=True)  # noqa

    resolution_notes = models.TextField(null=True, blank=True)
    # resolution_date = models.DateTimeField(null=True)

    class Meta:
        db_table = 'service_tickets'
