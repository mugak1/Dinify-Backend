from django.db import transaction
from restaurants_app.models import MenuSection
from users_app.models import User


class ConMenuSection:
    def __init__(self):
        pass

    def reorder_listing(
        self,
        ordering: list,
        user: User
    ) -> dict:
        with transaction.atomic():
            for index, section_id in enumerate(ordering):
                section = MenuSection.objects.get(id=section_id)
                section.listing_position = index
                section.save()
        return {
            'status': 200,
            'message': 'Menu sections reordered successfully'
        }
