from django.db import models


# Eod Status
# 0 = pending, 1 = progress, 1 = completed, 2 = failed, 4 = cancelled

# Create your models here.
class SysActivityConfig(models.Model):
    id = models.AutoField(primary_key=True)
    config_name = models.CharField(max_length=255, unique=True)
    config_description = models.TextField(blank=True, null=True)
    config_type = models.CharField(max_length=50, choices=[
        ('boolean', 'Boolean'),
        ('integer', 'Integer'),
        ('string', 'String'),
        ('date', 'DateTime')
    ])
    config_boolean_value = models.BooleanField(default=False)
    config_integer_value = models.IntegerField(default=0)
    config_string_value = models.CharField(max_length=255, blank=True, null=True)
    config_date_value = models.DateField(blank=True)

    # eod_processing
    eod_last_date = models.DateField(null=True, db_index=True)
    eod_start_time = models.DateTimeField(null=True, db_index=True)
    eod_end_time = models.DateTimeField(null=True, db_index=True)
    eod_current_status = models.IntegerField(default=0)

    class Meta:
        db_table = 'sys_activity_config'
