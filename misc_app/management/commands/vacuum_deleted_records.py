from django.core.management.base import BaseCommand
from .vacuum_configuration import VACUUM_MODELS
from restaurants_app.models import DiningArea, Table, MenuSection, SectionGroup, MenuItem


class ConVacuumDeletedRecords:
    def __init__(self):
        pass

    def soft_cascade_under_dining_areas(self, dining_area):
        Table.objects.filter(dining_area=dining_area, deleted=False).update(deleted=True)

    def soft_cascade_under_sections(self, section):
        SectionGroup.objects.filter(section=section, deleted=False).update(deleted=True)
        MenuItem.objects.filter(section=section, deleted=False).update(deleted=True)

    def soft_cascade_under_groups(self, group):
        MenuItem.objects.filter(section_group=group, deleted=False).update(deleted=True)

    def vacuum(self):
        for model in VACUUM_MODELS:
            records_pending_vacuum = model['model'].objects.filter(
                deleted=True,
                vacuumed=False
            )
            print(f"{model['model'].__name__}: {records_pending_vacuum.count()}")

            # soft cascade deletes
            # if the model is dining area, delete the table under it
            run_soft_cascade_dining_areas = False
            run_soft_cascade_sections = False
            run_soft_cascade_groups = False

            if model['model'] == DiningArea:
                run_soft_cascade_dining_areas = True
            elif model['model'] == MenuSection:
                run_soft_cascade_sections = True
            elif model['model'] == SectionGroup:
                run_soft_cascade_groups = True

            for rec in records_pending_vacuum:
                if run_soft_cascade_dining_areas:
                    self.soft_cascade_under_dining_areas(dining_area=rec)
                elif run_soft_cascade_sections:
                    self.soft_cascade_under_sections(section=rec)
                elif run_soft_cascade_groups:
                    self.soft_cascade_under_groups(group=rec)

                filters = {
                    'deleted': True,
                    'vacuumed': True,
                }

                ignore_str_fields = ['str_number']
                for field, value in model['unique_fields'][0].items():
                    if value not in ignore_str_fields:
                        filters[value] = getattr(rec, field)
                count_prior_vacuums = model['model'].objects.filter(**filters).count()
                deletion_count = count_prior_vacuums + 1

                # rename the record
                # if the rename field is null, then just add, else add a suffix
                if getattr(rec, model['rename_field']) is None:
                    new_name = f"{model['model'].__name__}_autodel{deletion_count}"
                else:
                    new_name = f"{getattr(rec, model['rename_field'])}_autodel{deletion_count}"
                print(f"\n{filters} {new_name}\n")

                # setattr(rec, model['rename_field'], new_name)
                # rec.vacuumed = True
                # rec.save()

                # TODO save action to mongodb as a log


class Command(BaseCommand):
    help = "Alters the 'renameable fields' of deleted records and sets a flag of vacuum on them."

    def handle(self, *args, **options):
        ConVacuumDeletedRecords().vacuum()
