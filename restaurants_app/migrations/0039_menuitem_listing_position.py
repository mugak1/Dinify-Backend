from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants_app', '0038_fix_corrupted_jsonfields'),
    ]

    operations = [
        migrations.AddField(
            model_name='menuitem',
            name='listing_position',
            field=models.IntegerField(default=0),
        ),
        migrations.AlterModelOptions(
            name='menuitem',
            options={'ordering': ['section', 'listing_position', 'name']},
        ),
    ]
