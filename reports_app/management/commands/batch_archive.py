from django.core.management.base import BaseCommand
from users_app.models import User, SerArcUser
from misc_app.controllers.utils.archive_record import archive_record


class Command(BaseCommand):
    help = """
    - Archive all specified documents
    """

    def handle(self, *args, **options):
        all_users = User.objects.all()

        for user in all_users:
            record_data = SerArcUser(user).data
            record_data['time_created'] = record_data['date_joined']
            archive_record(
                record_data=record_data,
                archive_collection='archive_users',
            )
