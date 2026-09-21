from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F

from bookmarks.models import BookmarkBundle, User

# Max. number of attempts when a concurrent change causes a conflict,
# e.g. SQLite's "database is locked" or a unique-order collision that
# slipped past the row locks. Each retry runs in a fresh transaction, so
# a failed attempt never leaves a created bundle or a half-completed shift.
MAX_CONFLICT_RETRIES = 5


class InvalidBundleOrder(ValueError):
    """Raised when a requested bundle position is outside the valid range."""


def create_bundle(
    bundle: BookmarkBundle,
    current_user: User,
    target_order: int | None = None,
) -> BookmarkBundle:
    return _run_with_conflict_retry(
        lambda: _create_bundle(bundle, current_user, target_order)
    )


def move_bundle(bundle_to_move: BookmarkBundle, new_order: int) -> None:
    _run_with_conflict_retry(lambda: _move_bundle(bundle_to_move, new_order))


def update_bundle(
    bundle: BookmarkBundle, target_order: int | None
) -> BookmarkBundle:
    return _run_with_conflict_retry(lambda: _update_bundle(bundle, target_order))


def delete_bundle(bundle: BookmarkBundle) -> None:
    _run_with_conflict_retry(lambda: _delete_bundle(bundle))


def _create_bundle(
    bundle: BookmarkBundle,
    current_user: User,
    target_order: int | None,
) -> BookmarkBundle:
    with transaction.atomic():
        siblings = _lock_user_bundles(current_user)
        count = len(siblings)

        # A new bundle can be inserted anywhere from 0 (front) to
        # count (end / append).
        if target_order is None:
            target_order = count
        _validate_position(target_order, count, allow_append=True)

        # Shift all existing orders out of the way first, so the unique
        # (owner, order) constraint can never be violated while the new
        # sequence is written, even if the database updates rows one at a
        # time within a single statement. The offset must place the
        # shifted orders above every possible final order, i.e. above the
        # highest current order, the new bundle's target, and the count.
        if siblings:
            sibling_pks = [sibling.pk for sibling in siblings]
            max_order = max(sibling.order for sibling in siblings)
            offset = max(max_order, target_order, count) + 1
            BookmarkBundle.objects.filter(pk__in=sibling_pks).update(
                order=F("order") + offset
            )

        bundle.owner = current_user
        bundle.order = target_order
        bundle.save()

        for index, sibling in enumerate(siblings):
            sibling.order = index if index < target_order else index + 1
        if siblings:
            BookmarkBundle.objects.bulk_update(siblings, ["order"])

        return bundle


def _move_bundle(bundle_to_move: BookmarkBundle, new_order: int) -> None:
    with transaction.atomic():
        siblings = _lock_user_bundles(bundle_to_move.owner_id)
        count = len(siblings)

        # An existing bundle can only move within the current sequence,
        # i.e. 0 to count - 1.
        _validate_position(new_order, count, allow_append=False)

        current_index = _find_bundle_index(siblings, bundle_to_move)
        if current_index != new_order:
            ordered_bundles = siblings.copy()
            ordered_bundles.pop(current_index)
            ordered_bundles.insert(new_order, bundle_to_move)
            _renumber(ordered_bundles)


def _update_bundle(
    bundle: BookmarkBundle, target_order: int | None
) -> BookmarkBundle:
    with transaction.atomic():
        siblings = _lock_user_bundles(bundle.owner_id)
        count = len(siblings)

        if target_order is not None:
            _validate_position(target_order, count, allow_append=False)

            current_index = _find_bundle_index(siblings, bundle)
            if current_index != target_order:
                ordered_bundles = siblings.copy()
                ordered_bundles.pop(current_index)
                ordered_bundles.insert(target_order, bundle)
                _renumber(ordered_bundles)

        # Persist the other field changes together with the reordering,
        # so either everything is committed or nothing is.
        bundle.save()
        return bundle


def _delete_bundle(bundle: BookmarkBundle) -> None:
    with transaction.atomic():
        siblings = _lock_user_bundles(bundle.owner_id)
        remaining_bundles = [
            sibling for sibling in siblings if sibling.pk != bundle.pk
        ]

        bundle.delete()
        _renumber(remaining_bundles)


def _lock_user_bundles(owner) -> list[BookmarkBundle]:
    # Lock all bundles of the user in a deterministic order. On databases
    # that support it, SELECT FOR UPDATE blocks concurrent transactions
    # until the lock is released, forcing same-user changes into a complete
    # serial order. On SQLite the lock is a no-op, but database writes are
    # serialized anyway.
    return list(
        BookmarkBundle.objects.filter(owner=owner)
        .order_by("order", "date_created", "id")
        .select_for_update()
    )


def _validate_position(
    position: int, count: int, allow_append: bool
) -> None:
    upper_bound = count if allow_append else count - 1
    if not isinstance(position, int) or isinstance(position, bool):
        raise InvalidBundleOrder("Order must be an integer.")
    if position < 0 or position > upper_bound:
        raise InvalidBundleOrder(
            f"Order must be between 0 and {upper_bound}."
        )


def _find_bundle_index(
    bundles: list[BookmarkBundle], bundle: BookmarkBundle
) -> int:
    for index, candidate in enumerate(bundles):
        if candidate.pk == bundle.pk:
            return index
    raise InvalidBundleOrder("Bundle does not exist.")


def _renumber(ordered_bundles: list[BookmarkBundle]) -> None:
    """Persist the given order as a dense 0..n-1 sequence.

    Existing orders are first shifted above the highest current order, so
    writing the final orders can never transiently violate the unique
    (owner, order) constraint, even when the database updates rows one at
    a time within a single statement. Neither QuerySet.update nor
    bulk_update touch auto_now fields, so date_modified is not changed by
    reordering.
    """
    pks = [bundle.pk for bundle in ordered_bundles]
    if not pks:
        return

    offset = max(bundle.order for bundle in ordered_bundles) + 1
    BookmarkBundle.objects.filter(pk__in=pks).update(
        order=F("order") + offset
    )

    for index, bundle in enumerate(ordered_bundles):
        bundle.order = index
    BookmarkBundle.objects.bulk_update(ordered_bundles, ["order"])


def _run_with_conflict_retry(operation):
    for attempt in range(MAX_CONFLICT_RETRIES):
        try:
            return operation()
        except (OperationalError, IntegrityError):
            if attempt + 1 >= MAX_CONFLICT_RETRIES:
                raise
