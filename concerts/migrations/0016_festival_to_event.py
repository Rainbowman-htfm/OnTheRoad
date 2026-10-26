import django.db.models.deletion
from django.db import migrations, models


def rename_permissions(apps, schema_editor, old="festival", new="event"):
    """Django renames the table and its content type, not the permissions' codes ("add_festival"): without
    this, band members would lose the right to add one."""
    Permission = apps.get_model("auth", "Permission")
    for permission in Permission.objects.filter(content_type__app_label="concerts", codename__endswith=f"_{old}"):
        permission.codename = permission.codename.removesuffix(old) + new
        permission.name = permission.name.replace(old, new)
        permission.save()


def rename_permissions_back(apps, schema_editor):
    rename_permissions(apps, schema_editor, old="event", new="festival")


class Migration(migrations.Migration):
    """A festival or a night with a name ("Concert 100% Girls"): "Festival" becomes "Event". The data stays."""

    dependencies = [
        ("concerts", "0015_other_bands"),
        ("auth", "__latest__"),
    ]

    operations = [
        migrations.RenameModel("Festival", "Event"),
        migrations.RenameField("concert", "festival", "event"),
        migrations.AlterField(
            model_name="concert",
            name="event",
            field=models.ForeignKey(
                blank=True,
                help_text="A festival, or a night with a name: shown before the venue.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="concerts",
                to="concerts.event",
            ),
        ),
        migrations.RunPython(rename_permissions, rename_permissions_back),
    ]
