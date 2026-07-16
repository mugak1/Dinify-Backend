import logging

from dinify_backend.mongo_db import MONGO_DB, COL_NOTIFICATIONS
from bson import ObjectId

logger = logging.getLogger(__name__)


def _recipient_or_clauses(email, phone):
    """Build the tos/ccs recipient ``$or`` clauses from the caller's REAL identities.

    Both the read sink (``get_notifications``) and the mark-read sink
    (``flag_notification_as_read``) scope to a notification's recipients by
    matching ``tos``/``ccs`` against the requesting user's email and phone. Those
    identities are NOT guaranteed present — ``User.email`` is nullable — so a raw
    ``{'tos': email}`` predicate built from ``None``/``''`` degenerates into a
    match-anything / null-recipient equality and leaks (read) or lets a user flag
    (write) notifications they do not own.

    This helper normalises the identities (drop ``None`` and any non-string, strip
    then drop empty/whitespace-only, de-duplicate) and returns the ordered clause
    list ``[{'tos': id}, ... , {'ccs': id}, ...]``. An EMPTY list means the caller
    has no usable identity — callers MUST treat that as "owns nothing" and issue NO
    Mongo query, so ``None``/``''`` can never reach a recipient predicate. Both
    sinks use this single builder so their ownership semantics cannot drift.
    """
    identities = []
    for value in (email, phone):
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in identities:
            continue
        identities.append(cleaned)

    return (
        [{'tos': identity} for identity in identities]
        + [{'ccs': identity} for identity in identities]
    )


def get_notifications(
    email: str,
    phone: str,
    skip_read: bool = False,
    skip_archived: bool = True
):
    try:
        # find notifications where the email is incluced in the tos
        # or the phone is included in the tos
        # or the email is included in the ccs
        # or the phone is included in the ccs
        #
        clauses = _recipient_or_clauses(email, phone)
        if not clauses:
            # No real identity → the caller owns nothing. Return early WITHOUT
            # touching Mongo, so an empty/None identity never becomes a
            # match-anything recipient predicate.
            return []

        filter = {'$or': clauses}
        if skip_read:
            filter['read'] = {'$exists': False}
        if skip_archived:
            filter['archived'] = {'$exists': False}

        notifications = MONGO_DB[COL_NOTIFICATIONS].find(filter=filter)

        notifications = list(notifications)

        # Convert ObjectId to string
        for notification in notifications:
            notification['_id'] = str(notification['_id'])

        return notifications

    except Exception as error:
        logger.error("Error while getting notifications: %s", error)
        return []


def flag_notification_as_read(notification_id: str, email: str, phone: str):
    try:
        # Scope the write to a notification the requesting user actually
        # receives — the SAME recipient predicate get_notifications uses on
        # the read side (email/phone present in the tos/ccs arrays). Filtering
        # on _id alone would let any authenticated user flag any notification.
        clauses = _recipient_or_clauses(email, phone)
        if not clauses:
            # No real identity → the caller cannot own (or mark) any
            # notification. Return early WITHOUT touching Mongo, so an
            # empty/None identity never scopes a write.
            return False

        filter = {
            '_id': ObjectId(notification_id),
            '$or': clauses,
        }
        result = MONGO_DB[COL_NOTIFICATIONS].update_one(
            filter=filter,
            update={'$set': {'read': True}}
        )
        # matched_count (not modified_count) so re-flagging an already-read
        # notification the user owns still counts as success.
        return result.matched_count > 0
    except Exception as error:
        logger.error("Error while flagging notification as read: %s", error)
        return False
