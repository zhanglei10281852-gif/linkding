from django.db import migrations, models
from django.db.models import F, Q


def repair_bundle_orders(apps, schema_editor):
    BookmarkBundle = apps.get_model("bookmarks", "BookmarkBundle")

    owner_ids = (
        BookmarkBundle.objects.order_by("owner_id")
        .values_list("owner_id", flat=True)
        .distinct()
    )

    for owner_id in owner_ids:
        # Stable ordering: keep the original order first, and use the
        # creation date and id only to break ties between duplicates.
        bundles = list(
            BookmarkBundle.objects.filter(owner_id=owner_id).order_by(
                "order", "date_created", "id"
            )
        )
        count = len(bundles)
        if count == 0:
            continue

        # Shift existing orders above the target range before writing the
        # dense sequence, to avoid transient collisions even on databases
        # that enforce constraints during the migration.
        BookmarkBundle.objects.filter(owner_id=owner_id).update(
            order=F("order") + count + 1
        )

        for index, bundle in enumerate(bundles):
            bundle.order = index
        BookmarkBundle.objects.bulk_update(bundles, ["order"])


def noop_reverse(apps, schema_editor):
    # The repair cannot be reversed: the original (possibly invalid)
    # orders are not preserved.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("bookmarks", "0054_bookmarkbundle_filter_shared_and_more"),
    ]

    operations = [
        migrations.RunPython(repair_bundle_orders, noop_reverse),
        migrations.AddConstraint(
            model_name="bookmarkbundle",
            constraint=models.UniqueConstraint(
                fields=("owner", "order"),
                name="bookmarks_bookmarkbundle_owner_order_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookmarkbundle",
            constraint=models.CheckConstraint(
                condition=Q(order__gte=0),
                name="bookmarks_bookmarkbundle_order_non_negative",
            ),
        ),
    ]
