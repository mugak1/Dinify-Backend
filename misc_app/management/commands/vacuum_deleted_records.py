from django.core.management.base import BaseCommand
from django.core.management import CommandError
from .vacuum_configuration import VACUUM_MODELS


class Command(BaseCommand):
    help = "Alters the 'renameable fields' of deleted records and sets a flag of vacuum on them."

    def handle(self, *args, **options):
        for model in VACUUM_MODELS:
            records_pending_vacuum = model['model'].objects.filter(
                deleted=True,
                vacuumed=False
            )

            for rec in records_pending_vacuum:
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
