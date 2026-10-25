from django.db import migrations, models


def from_free_entry(apps, schema_editor):
    apps.get_model("concerts", "Concert").objects.filter(free_entry=True).update(entry="free")


def to_free_entry(apps, schema_editor):
    apps.get_model("concerts", "Concert").objects.filter(entry="free").update(free_entry=True)


class Migration(migrations.Migration):
    """The "free entry" box becomes one choice: tickets, free entry or pay what you can. The source field
    goes: the link is the ticket page, or the event's page when entry is free or pay what you can."""

    dependencies = [
        ("concerts", "0012_concert_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="concert",
            name="entry",
            field=models.CharField(
                choices=[("tickets", "Tickets"), ("free", "free entry"), ("pay_what_you_can", "pay what you can")],
                default="tickets",
                max_length=20,
            ),
        ),
        migrations.RunPython(from_free_entry, to_free_entry),
        migrations.RemoveField(model_name="concert", name="free_entry"),
        # One link instead: the ticket page, or the event's page for free and pay-what-you-can concerts
        migrations.RemoveField(model_name="concert", name="source_url"),
        migrations.AlterField(
            model_name="concert",
            name="ticket_url",
            field=models.URLField(
                blank=True,
                help_text="Tickets: the ticket page. Otherwise the event's page or announcement.",
                verbose_name="link",
            ),
        ),
    ]
