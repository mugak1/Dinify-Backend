"""
Idempotent data migration backfilling default role-permission rows for every
existing restaurant.

Belt-and-suspenders: the permission resolver already falls back to the coded
defaults (restaurants_app.configs.role_defaults.DEFAULT_ROLE_MODULES) when no
RestaurantRolePermission row exists, so the system is correct even before any
rows exist — but persisting the four default rows per restaurant makes each
role's grid visible/editable in the portal.

The default grids are inlined here as plain dict literals (mirroring
DEFAULT_ROLE_MODULES) so the migration stays stable against future edits of the
config module — the same approach 0045 used for SYSTEM_PRESET_TAGS.

Reverse: best-effort — removes every RestaurantRolePermission row.
"""
from django.db import migrations


# Mirrors restaurants_app.configs.role_defaults.DEFAULT_ROLE_MODULES. Inlined so
# the migration is stable against future edits of that module.
GRID_MODULES = [
    'dashboard', 'kitchen', 'tables', 'menu', 'reviews', 'reports', 'settings',
]


def _all_grid(value):
    return {module: value for module in GRID_MODULES}


def _grid_only(*enabled):
    grid = _all_grid(False)
    for module in enabled:
        grid[module] = True
    return grid


DEFAULT_ROLE_MODULES = {
    'owner':            _all_grid(True),
    'manager':          _all_grid(True),
    'kitchen':          _grid_only('kitchen'),
    'restaurant_staff': _grid_only('tables'),
}


def backfill(apps, schema_editor):
    Restaurant = apps.get_model('restaurants_app', 'Restaurant')
    RestaurantRolePermission = apps.get_model(
        'restaurants_app', 'RestaurantRolePermission'
    )

    seeded_count = 0
    for restaurant in Restaurant.objects.iterator(chunk_size=200):
        for role, modules in DEFAULT_ROLE_MODULES.items():
            _, created = RestaurantRolePermission.objects.get_or_create(
                restaurant=restaurant,
                role=role,
                defaults={'modules': dict(modules)},
            )
            if created:
                seeded_count += 1

    print(f'[0053 migration] seeded={seeded_count} role-permission rows')


def reverse_backfill(apps, schema_editor):
    RestaurantRolePermission = apps.get_model(
        'restaurants_app', 'RestaurantRolePermission'
    )
    RestaurantRolePermission.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants_app', '0052_restaurantrolepermission'),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_backfill),
    ]
