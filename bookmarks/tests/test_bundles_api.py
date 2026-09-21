import importlib

from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from bookmarks.models import BookmarkBundle
from bookmarks.tests.helpers import BookmarkFactoryMixin, LinkdingApiTestCase


class BundlesApiTestCase(LinkdingApiTestCase, BookmarkFactoryMixin):
    def assertBundle(self, bundle: BookmarkBundle, data: dict):
        self.assertEqual(bundle.id, data["id"])
        self.assertEqual(bundle.name, data["name"])
        self.assertEqual(bundle.search, data["search"])
        self.assertEqual(bundle.any_tags, data["any_tags"])
        self.assertEqual(bundle.all_tags, data["all_tags"])
        self.assertEqual(bundle.excluded_tags, data["excluded_tags"])
        self.assertEqual(bundle.filter_unread, data["filter_unread"])
        self.assertEqual(bundle.filter_shared, data["filter_shared"])
        self.assertEqual(bundle.order, data["order"])
        self.assertEqual(
            bundle.date_created.isoformat().replace("+00:00", "Z"), data["date_created"]
        )
        self.assertEqual(
            bundle.date_modified.isoformat().replace("+00:00", "Z"),
            data["date_modified"],
        )

    def test_bundle_list(self):
        self.authenticate()

        bundles = [
            self.setup_bundle(name="Bundle 1", order=0),
            self.setup_bundle(name="Bundle 2", order=1),
            self.setup_bundle(name="Bundle 3", order=2),
        ]

        url = reverse("linkding:bundle-list")
        response = self.get(url, expected_status_code=status.HTTP_200_OK)

        self.assertEqual(len(response.data["results"]), 3)
        self.assertBundle(bundles[0], response.data["results"][0])
        self.assertBundle(bundles[1], response.data["results"][1])
        self.assertBundle(bundles[2], response.data["results"][2])

    def test_bundle_list_only_returns_own_bundles(self):
        self.authenticate()

        user_bundles = [
            self.setup_bundle(name="User Bundle 1"),
            self.setup_bundle(name="User Bundle 2"),
        ]

        other_user = self.setup_user()
        self.setup_bundle(name="Other User Bundle 1", user=other_user)
        self.setup_bundle(name="Other User Bundle 2", user=other_user)

        url = reverse("linkding:bundle-list")
        response = self.get(url, expected_status_code=status.HTTP_200_OK)

        self.assertEqual(len(response.data["results"]), 2)
        self.assertBundle(user_bundles[0], response.data["results"][0])
        self.assertBundle(user_bundles[1], response.data["results"][1])

    def test_bundle_list_requires_authentication(self):
        url = reverse("linkding:bundle-list")
        self.get(url, expected_status_code=status.HTTP_401_UNAUTHORIZED)

    def test_bundle_detail(self):
        self.authenticate()

        bundle = self.setup_bundle(
            name="Test Bundle",
            search="test search",
            any_tags="tag1 tag2",
            all_tags="required-tag",
            excluded_tags="excluded-tag",
            filter_unread=BookmarkBundle.FILTER_STATE_YES,
            filter_shared=BookmarkBundle.FILTER_STATE_NO,
            order=5,
        )

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        response = self.get(url, expected_status_code=status.HTTP_200_OK)

        self.assertBundle(bundle, response.data)

    def test_bundle_detail_only_returns_own_bundles(self):
        self.authenticate()

        other_user = self.setup_user()
        other_bundle = self.setup_bundle(name="Other User Bundle", user=other_user)

        url = reverse("linkding:bundle-detail", kwargs={"pk": other_bundle.id})
        self.get(url, expected_status_code=status.HTTP_404_NOT_FOUND)

    def test_bundle_detail_requires_authentication(self):
        bundle = self.setup_bundle()
        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        self.get(url, expected_status_code=status.HTTP_401_UNAUTHORIZED)

    def test_create_bundle(self):
        self.authenticate()

        bundle_data = {
            "name": "New Bundle",
            "search": "test search",
            "any_tags": "tag1 tag2",
            "all_tags": "required-tag",
            "excluded_tags": "excluded-tag",
            "filter_unread": BookmarkBundle.FILTER_STATE_YES,
            "filter_shared": BookmarkBundle.FILTER_STATE_NO,
        }

        url = reverse("linkding:bundle-list")
        response = self.post(
            url, bundle_data, expected_status_code=status.HTTP_201_CREATED
        )

        bundle = BookmarkBundle.objects.get(id=response.data["id"])
        self.assertEqual(bundle.name, bundle_data["name"])
        self.assertEqual(bundle.search, bundle_data["search"])
        self.assertEqual(bundle.any_tags, bundle_data["any_tags"])
        self.assertEqual(bundle.all_tags, bundle_data["all_tags"])
        self.assertEqual(bundle.excluded_tags, bundle_data["excluded_tags"])
        self.assertEqual(bundle.filter_unread, bundle_data["filter_unread"])
        self.assertEqual(bundle.filter_shared, bundle_data["filter_shared"])
        self.assertEqual(bundle.owner, self.user)
        self.assertEqual(bundle.order, 0)

        self.assertBundle(bundle, response.data)

    def test_create_bundle_appends_to_end_and_compacts_existing_orders(self):
        self.authenticate()

        # Factory setup bypasses the service, leaving a hole, but the
        # create operation must keep (and restore) a dense sequence.
        existing_bundle = self.setup_bundle(name="Existing Bundle", order=2)

        bundle_data = {"name": "New Bundle", "search": "test search"}

        url = reverse("linkding:bundle-list")
        response = self.post(
            url, bundle_data, expected_status_code=status.HTTP_201_CREATED
        )

        bundle = BookmarkBundle.objects.get(id=response.data["id"])
        existing_bundle.refresh_from_db()
        self.assertEqual(existing_bundle.order, 0)
        self.assertEqual(bundle.order, 1)

    def test_create_bundle_with_valid_order_inserts_and_shifts_siblings(self):
        self.authenticate()

        url = reverse("linkding:bundle-list")

        # Insert at position 0 on an empty list
        response = self.post(
            url,
            {"name": "Bundle 1", "order": 0},
            expected_status_code=status.HTTP_201_CREATED,
        )
        bundle1 = BookmarkBundle.objects.get(id=response.data["id"])
        self.assertEqual(bundle1.order, 0)

        # Insert another bundle at position 0, shifts the first one
        response = self.post(
            url,
            {"name": "Bundle 2", "order": 0},
            expected_status_code=status.HTTP_201_CREATED,
        )
        bundle2 = BookmarkBundle.objects.get(id=response.data["id"])
        self.assertEqual(bundle2.order, 0)
        bundle1.refresh_from_db()
        self.assertEqual(bundle1.order, 1)

        # Insert between the two bundles
        response = self.post(
            url,
            {"name": "Bundle 3", "order": 1},
            expected_status_code=status.HTTP_201_CREATED,
        )
        bundle3 = BookmarkBundle.objects.get(id=response.data["id"])
        self.assertEqual(bundle3.order, 1)
        bundle2.refresh_from_db()
        bundle1.refresh_from_db()
        self.assertEqual(bundle2.order, 0)
        self.assertEqual(bundle1.order, 2)

        # Append at the current count (3), which is a valid create position
        response = self.post(
            url,
            {"name": "Bundle 4", "order": 3},
            expected_status_code=status.HTTP_201_CREATED,
        )
        bundle4 = BookmarkBundle.objects.get(id=response.data["id"])
        self.assertEqual(bundle4.order, 3)

        # All orders form a dense 0..n-1 sequence
        orders = list(
            BookmarkBundle.objects.filter(owner=self.user)
            .order_by("order")
            .values_list("order", flat=True)
        )
        self.assertEqual(orders, [0, 1, 2, 3])

    def test_create_bundle_with_order_out_of_range_returns_400(self):
        self.authenticate()

        self.setup_bundle(name="Bundle 1", order=0)

        url = reverse("linkding:bundle-list")

        # Position 2 exceeds the current count (1)
        response = self.post(
            url,
            {"name": "Invalid Bundle", "order": 2},
            expected_status_code=status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn("order", response.data)

        # No bundle was created and existing records are unchanged
        self.assertEqual(
            BookmarkBundle.objects.filter(owner=self.user).count(), 1
        )
        existing = BookmarkBundle.objects.get(name="Bundle 1")
        self.assertEqual(existing.order, 0)

    def test_create_bundle_with_negative_order_returns_400(self):
        self.authenticate()

        url = reverse("linkding:bundle-list")
        response = self.post(
            url,
            {"name": "Invalid Bundle", "order": -1},
            expected_status_code=status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn("order", response.data)
        self.assertFalse(
            BookmarkBundle.objects.filter(name="Invalid Bundle").exists()
        )

    def test_create_bundle_requires_name(self):
        self.authenticate()

        bundle_data = {"search": "test search"}

        url = reverse("linkding:bundle-list")
        self.post(url, bundle_data, expected_status_code=status.HTTP_400_BAD_REQUEST)

    def test_create_bundle_fields_can_be_empty(self):
        self.authenticate()

        bundle_data = {
            "name": "Minimal Bundle",
            "search": "",
            "any_tags": "",
            "all_tags": "",
            "excluded_tags": "",
        }

        url = reverse("linkding:bundle-list")
        response = self.post(
            url, bundle_data, expected_status_code=status.HTTP_201_CREATED
        )

        bundle = BookmarkBundle.objects.get(id=response.data["id"])
        self.assertEqual(bundle.name, "Minimal Bundle")
        self.assertEqual(bundle.search, "")
        self.assertEqual(bundle.any_tags, "")
        self.assertEqual(bundle.all_tags, "")
        self.assertEqual(bundle.excluded_tags, "")

    def test_create_bundle_requires_authentication(self):
        bundle_data = {"name": "New Bundle"}

        url = reverse("linkding:bundle-list")
        self.post(url, bundle_data, expected_status_code=status.HTTP_401_UNAUTHORIZED)

    def test_update_bundle_put(self):
        self.authenticate()

        bundle = self.setup_bundle(
            name="Original Bundle",
            search="original search",
            any_tags="original-tag",
            order=0,
        )
        self.setup_bundle(name="Second Bundle", order=1)
        self.setup_bundle(name="Third Bundle", order=2)

        updated_data = {
            "name": "Updated Bundle",
            "search": "updated search",
            "any_tags": "updated-tag1 updated-tag2",
            "all_tags": "required-updated-tag",
            "excluded_tags": "excluded-updated-tag",
            "filter_unread": BookmarkBundle.FILTER_STATE_YES,
            "filter_shared": BookmarkBundle.FILTER_STATE_NO,
            "order": 2,
        }

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        response = self.put(url, updated_data, expected_status_code=status.HTTP_200_OK)

        bundle.refresh_from_db()
        self.assertEqual(bundle.name, updated_data["name"])
        self.assertEqual(bundle.search, updated_data["search"])
        self.assertEqual(bundle.any_tags, updated_data["any_tags"])
        self.assertEqual(bundle.all_tags, updated_data["all_tags"])
        self.assertEqual(bundle.excluded_tags, updated_data["excluded_tags"])
        self.assertEqual(bundle.filter_unread, updated_data["filter_unread"])
        self.assertEqual(bundle.filter_shared, updated_data["filter_shared"])
        self.assertEqual(bundle.order, updated_data["order"])

        # Siblings were shifted and the sequence stays dense
        orders = list(
            BookmarkBundle.objects.filter(owner=self.user)
            .order_by("order")
            .values_list("name", "order")
        )
        self.assertEqual(
            orders,
            [("Second Bundle", 0), ("Third Bundle", 1), ("Updated Bundle", 2)],
        )

        self.assertBundle(bundle, response.data)

    def test_update_bundle_patch_with_order_moves_bundle(self):
        self.authenticate()

        self.setup_bundle(name="Bundle 1", order=0)
        bundle2 = self.setup_bundle(name="Bundle 2", order=1)
        self.setup_bundle(name="Bundle 3", order=2)

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle2.id})
        response = self.patch(
            url, {"order": 0}, expected_status_code=status.HTTP_200_OK
        )

        bundle2.refresh_from_db()
        self.assertEqual(bundle2.order, 0)

        orders = list(
            BookmarkBundle.objects.filter(owner=self.user)
            .order_by("order")
            .values_list("name", "order")
        )
        self.assertEqual(
            orders,
            [("Bundle 2", 0), ("Bundle 1", 1), ("Bundle 3", 2)],
        )
        self.assertEqual(response.data["order"], 0)

    def test_update_bundle_patch_without_order_keeps_position(self):
        self.authenticate()

        bundle = self.setup_bundle(
            name="Original Bundle",
            search="original search",
            any_tags="original-tag",
            order=1,
        )
        self.setup_bundle(name="Other Bundle", order=0)

        updated_data = {
            "name": "Partially Updated Bundle",
            "search": "partially updated search",
        }

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        response = self.patch(
            url, updated_data, expected_status_code=status.HTTP_200_OK
        )

        bundle.refresh_from_db()
        self.assertEqual(bundle.name, updated_data["name"])
        self.assertEqual(bundle.search, updated_data["search"])
        self.assertEqual(bundle.any_tags, "original-tag")  # Unchanged
        self.assertEqual(bundle.order, 1)  # Position unchanged

        self.assertBundle(bundle, response.data)

    def test_update_bundle_with_order_out_of_range_returns_400(self):
        self.authenticate()

        bundle = self.setup_bundle(name="Bundle 1", order=0)

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        self.patch(
            url, {"order": 1}, expected_status_code=status.HTTP_400_BAD_REQUEST
        )

        # Nothing was changed
        bundle.refresh_from_db()
        self.assertEqual(bundle.name, "Bundle 1")
        self.assertEqual(bundle.order, 0)

    def test_update_bundle_with_negative_order_returns_400(self):
        self.authenticate()

        bundle = self.setup_bundle(name="Bundle 1", order=0)

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        self.patch(
            url, {"order": -1}, expected_status_code=status.HTTP_400_BAD_REQUEST
        )

        bundle.refresh_from_db()
        self.assertEqual(bundle.order, 0)

    def test_update_bundle_only_allows_own_bundles(self):
        self.authenticate()

        other_user = self.setup_user()
        other_bundle = self.setup_bundle(name="Other User Bundle", user=other_user)

        updated_data = {"name": "Updated Bundle"}

        url = reverse("linkding:bundle-detail", kwargs={"pk": other_bundle.id})
        self.put(url, updated_data, expected_status_code=status.HTTP_404_NOT_FOUND)

    def test_update_bundle_requires_authentication(self):
        bundle = self.setup_bundle()
        updated_data = {"name": "Updated Bundle"}

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        self.put(url, updated_data, expected_status_code=status.HTTP_401_UNAUTHORIZED)

    def test_delete_bundle(self):
        self.authenticate()

        bundle = self.setup_bundle(name="Bundle to Delete")

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        self.delete(url, expected_status_code=status.HTTP_204_NO_CONTENT)

        self.assertFalse(BookmarkBundle.objects.filter(id=bundle.id).exists())

    def test_delete_bundle_updates_order(self):
        self.authenticate()

        bundle1 = self.setup_bundle(name="Bundle 1", order=0)
        bundle2 = self.setup_bundle(name="Bundle 2", order=1)
        bundle3 = self.setup_bundle(name="Bundle 3", order=2)

        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle2.id})
        self.delete(url, expected_status_code=status.HTTP_204_NO_CONTENT)

        self.assertFalse(BookmarkBundle.objects.filter(id=bundle2.id).exists())

        # Check that the remaining bundles have updated orders
        bundle1.refresh_from_db()
        bundle3.refresh_from_db()
        self.assertEqual(bundle1.order, 0)
        self.assertEqual(bundle3.order, 1)

    def test_delete_bundle_only_allows_own_bundles(self):
        self.authenticate()

        other_user = self.setup_user()
        other_bundle = self.setup_bundle(name="Other User Bundle", user=other_user)

        url = reverse("linkding:bundle-detail", kwargs={"pk": other_bundle.id})
        self.delete(url, expected_status_code=status.HTTP_404_NOT_FOUND)

        self.assertTrue(BookmarkBundle.objects.filter(id=other_bundle.id).exists())

    def test_delete_bundle_requires_authentication(self):
        bundle = self.setup_bundle()
        url = reverse("linkding:bundle-detail", kwargs={"pk": bundle.id})
        self.delete(url, expected_status_code=status.HTTP_401_UNAUTHORIZED)

        self.assertTrue(BookmarkBundle.objects.filter(id=bundle.id).exists())

    def test_bundles_ordered_by_order_field(self):
        self.authenticate()

        self.setup_bundle(name="Third Bundle", order=2)
        self.setup_bundle(name="First Bundle", order=0)
        self.setup_bundle(name="Second Bundle", order=1)

        url = reverse("linkding:bundle-list")
        response = self.get(url, expected_status_code=status.HTTP_200_OK)

        self.assertEqual(len(response.data["results"]), 3)
        self.assertEqual(response.data["results"][0]["name"], "First Bundle")
        self.assertEqual(response.data["results"][1]["name"], "Second Bundle")
        self.assertEqual(response.data["results"][2]["name"], "Third Bundle")


class BundleOrderRepairMigrationTestCase(TestCase, BookmarkFactoryMixin):
    TABLE_NAME = BookmarkBundle._meta.db_table
    BACKUP_TABLE = f"{TABLE_NAME}__migration_backup"

    def setUp(self) -> None:
        self.get_or_create_test_user()

    def replace_table_with_unconstrained_copy(self):
        # The repair must run against a table without the constraints,
        # just like a database created before the migration. SQLite cannot
        # drop table constraints on older versions, so swap the real table
        # for an identical table without the constraint definitions.
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {self.TABLE_NAME} RENAME TO {self.BACKUP_TABLE}")
            cursor.execute(
                f"""
                CREATE TABLE {self.TABLE_NAME} (
                    id integer NOT NULL PRIMARY KEY AUTOINCREMENT,
                    name varchar(256) NOT NULL,
                    search varchar(256) NOT NULL,
                    any_tags varchar(1024) NOT NULL,
                    all_tags varchar(1024) NOT NULL,
                    excluded_tags varchar(1024) NOT NULL,
                    filter_unread varchar(3) NOT NULL,
                    filter_shared varchar(3) NOT NULL,
                    "order" integer NOT NULL,
                    date_created datetime NOT NULL,
                    date_modified datetime NOT NULL,
                    owner_id integer NOT NULL
                )
                """
            )

    def restore_original_table(self):
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE {self.TABLE_NAME}")
            cursor.execute(
                f"ALTER TABLE {self.BACKUP_TABLE} RENAME TO {self.TABLE_NAME}"
            )

    def test_repairs_negative_duplicate_and_gap_orders(self):
        bundle_a = self.setup_bundle(name="Bundle A", order=0)
        bundle_b = self.setup_bundle(name="Bundle B", order=1)
        bundle_c = self.setup_bundle(name="Bundle C", order=2)
        bundle_d = self.setup_bundle(name="Bundle D", order=3)

        self.replace_table_with_unconstrained_copy()

        try:
            # Copy the rows into the unconstrained table
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self.TABLE_NAME} (
                        id, name, search, any_tags, all_tags, excluded_tags,
                        filter_unread, filter_shared, "order",
                        date_created, date_modified, owner_id
                    )
                    SELECT
                        id, name, search, any_tags, all_tags, excluded_tags,
                        filter_unread, filter_shared, "order",
                        date_created, date_modified, owner_id
                    FROM {self.BACKUP_TABLE}
                    """
                )

            # Two negative duplicates, a gap (3 kept), and a far-away order
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE {self.TABLE_NAME} SET "order" = -5 WHERE id = %s',
                    (bundle_a.id,),
                )
                cursor.execute(
                    f'UPDATE {self.TABLE_NAME} SET "order" = -5 WHERE id = %s',
                    (bundle_b.id,),
                )
                cursor.execute(
                    f'UPDATE {self.TABLE_NAME} SET "order" = 10 WHERE id = %s',
                    (bundle_c.id,),
                )

            migration = importlib.import_module(
                "bookmarks.migrations.0055_bookmarkbundle_order_constraints"
            )
            migration.repair_bundle_orders(django_apps, None)

            # Stable order by (order, date_created, id): the two -5
            # bundles keep their relative order (A before B), then the
            # bundle at order 3 (D), then the one at 10 (C).
            with connection.cursor() as cursor:
                rows = cursor.execute(
                    f'SELECT id, "order" FROM {self.TABLE_NAME} ORDER BY "order"'
                ).fetchall()

            self.assertEqual(
                rows,
                [
                    (bundle_a.id, 0),
                    (bundle_b.id, 1),
                    (bundle_d.id, 2),
                    (bundle_c.id, 3),
                ],
            )
        finally:
            self.restore_original_table()
