from django.core.management.base import BaseCommand
from django.core.management import CommandError
from .vacuum_configuration import VACUUM_MODELS
from restaurants_app.models import DiningArea, Table


class Command(BaseCommand):
    help = "Alters the 'renameable fields' of deleted records and sets a flag of vacuum on them."

    def delete_tables_under_dining_areas(self, dining_area):
        # find deleted dining areas and delete the respective tables under them
        # deleted_dining_areas = DiningArea.objects.filter(deleted=True)
        # for dining_area in deleted_dining_areas:
            # tables = Table.objects.filter(dining_area=dining_area)
            # for table in tables:
            #     table.deleted = True
            #     table.save()
            # print(str(dining_area.pk))
        Table.objected.filter(
            dining_area=dining_area,
        ).update(deleted=True)


    def handle(self, *args, **options):
        for model in VACUUM_MODELS:
            records_pending_vacuum = model['model'].objects.filter(
                deleted=True,
                vacuumed=False
            )

            # if the model is dining area, delete the table under it
            if model == DiningArea:
                self.delete_tables_under_dining_areas(
                    dining_area=str()
                )
                delete_dining_areas = True

            for rec in records_pending_vacuum:
                if delete_dining_areas:
                    self.delete_tables_under_dining_areas(dining_area=rec)

                filters = {
                    'deleted': True,
                    'vacuumed': True,
                }
                for field, value in model['unique_fields'][0].items():
                    filters[value] = getattr(rec, field)
                # print(filters)

                count_prior_vacuums = model['model'].objects.filter(**filters).count()
                deletion_count = count_prior_vacuums + 1

                # rename the record
                # if the rename field is null, then just add, else add a suffix
                if getattr(rec, model['rename_field']) is None:
                    new_name = f"{model['model'].__name__}_autodel{deletion_count}"
                else:
                    new_name = f"{getattr(rec, model['rename_field'])}_autodel{deletion_count}"
                setattr(rec, model['rename_field'], new_name)
                rec.vacuumed = True
                rec.save()

                # TODO save action to mongodb as a log
