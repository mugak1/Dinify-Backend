"""
implementation for crud functions to the database
"""
import copy
import logging
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist, ValidationError

logger = logging.getLogger(__name__)
from django.utils import timezone
from dataclasses import dataclass
from dinify_backend.configs import (
    IGNORE_LOG_FIELDS, STRINGIFY_LOG_FIELDS,
    ACTION_LOG_STATUSES
)
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.check_required_information import check_required_information
from misc_app.controllers.paginator import DinifyPaginator
from misc_app.controllers.determine_changes import determine_changes
from misc_app.controllers.save_action_log import save_action
from misc_app.management.commands.vacuum_deleted_records import ConVacuumDeletedRecords
from restaurants_app.models import Restaurant
from users_app.models import User
from misc_app.controllers.notifications.notification import Notification
from misc_app.controllers.con_class_utils import ConMiscUtils


RECIPIENT_GREETED_MSG_TYPES = frozenset({'new-restaurant-employee'})


def make_notification_for_new_entry(
    restaurant_id: str,
    user: User,
    item_name: str,
    msg_type: str,
    record=None,
):
    """
    Build a notification msg_data for a newly-created entity.

    Two contracts are supported, selected by msg_type:

    - **Recipient-greeted** (msg_type in RECIPIENT_GREETED_MSG_TYPES):
        the email greets the new entity's owner by first name.
        msg_data shape: {msg_type, restaurant_name, restaurant_id,
                         first_name, user_id}.
        Requires `record` (with `.instance`) to extract recipient
        identity.

    - **Restaurant-greeted** (default):
        the email greets the restaurant by name and mentions the
        actor's full name.
        msg_data shape: {msg_type, restaurant_name, restaurant_id,
                         user, item_name}.
    """
    if restaurant_id is None:
        return

    restaurant_name = Restaurant.objects.values('name').get(
        id=restaurant_id
    )['name']

    if msg_type in RECIPIENT_GREETED_MSG_TYPES:
        if record is None or getattr(record, 'instance', None) is None:
            logger.warning(
                "Notification skipped (msg_type=%s): "
                "recipient-greeted contract requires record.instance",
                msg_type,
            )
            return
        recipient = record.instance.user
        msg_data = {
            'msg_type': msg_type,
            'restaurant_name': restaurant_name,
            'restaurant_id': restaurant_id,
            'first_name': recipient.first_name,
            'user_id': str(recipient.id),
        }
    else:
        msg_data = {
            'msg_type': msg_type,
            'restaurant_name': restaurant_name,
            'restaurant_id': restaurant_id,
            'user': f'{user.first_name} {user.last_name}',
            'item_name': item_name,
        }

    Notification(msg_data).create_notification()


@dataclass
class Secretary:
    """
    Class which handles general db crud operations
    """
    args: dict

    def __post_init__(self):
        """
        initialise the global variables for consideration
        """
        self.serializer = self.args.get('serializer')
        self.model_name = self.serializer.Meta.model.__name__
        self.ok_message = self.args.get('success_message')
        self.user_id = self.args.get('user_id')
        self.username = self.args.get('username')
        self.data = self.args.get('data')
        self.required_information = self.args.get('required_information')
        self.user = self.args.get('user')
        self.msg_type = self.args.get('msg_type')

        # Trusted server-derived values written via serializer.save() kwargs on
        # create (e.g. a resolved parent restaurant), never taken from request
        # data. created_by is always injected from the resolved actor below.
        self.server_values = self.args.get('server_values') or {}

        # Authoritative, server-built queryset defining the object universe an
        # update/delete may touch. REQUIRED for update() and delete(): they
        # resolve the row ONLY through this queryset (locked), never through an
        # unrestricted Model.objects.get. A missing queryset fails closed.
        self.instance_queryset = self.args.get('instance_queryset')

        # for non unique handling
        self.non_unique_handling = self.args.get('non_unique_handling')

    def formulate_log_data(self) -> dict:
        """
        make the data to be saved to the action logs
        """
        # construct the submitted data to save to the logs
        submitted_data = self.args['data'].copy()

        # if a key is in the fields to ignore list, remove it from the submitted data
        for key in IGNORE_LOG_FIELDS:
            try:
                del submitted_data[key]
            except KeyError:
                pass

        # if a key is in the fields to stringify list, attempt to make it a string
        for key in STRINGIFY_LOG_FIELDS:
            try:
                submitted_data[key] = str(submitted_data[key])
            except KeyError:
                pass

        return submitted_data

    def create(self):
        """
        - Creates a record in the database
        - Expects args dict to be like: `{
            'serializer': SerializerClass,
            'required_info': [required_information],
            'data': {data},
            'user_id': str(user_id),
            'username': str(username),
            'success_message': str(success_message),
            'error_message': str(error_message)
        }`
        """
        with transaction.atomic():
            log_data = self.formulate_log_data()

            # check if the required information is present
            info_check = check_required_information(
                self.required_information,
                self.data
            )
            if not info_check['status']:
                # saved the attempted action to the logs
                save_action(
                    affected_model=self.model_name,
                    affected_record=None,
                    action='create',
                    narration=info_check['message'],
                    result=ACTION_LOG_STATUSES.get('failed'),
                    user_id=self.user_id,
                    username=self.username,
                    submitted_data=log_data,
                    changes=None
                )
                return {
                    'status': 400,
                    'message': info_check['message']
                }

            # check if a duplicate entry exists
            if self.non_unique_handling is not None:
                all_unique = ConMiscUtils.check_non_unique_conflicts(
                    model=self.serializer.Meta.model,
                    unique_combination=self.non_unique_handling.get('unique_combination'),
                    fks=self.non_unique_handling.get('fks'),
                    values=self.data,
                    error_message=self.non_unique_handling.get('error_message'),
                    # existing_record_id=self.data.get('id')
                )

                if not all_unique['status'] == 200:
                    return {
                        'status': 400,
                        'message': all_unique['message']
                    }

            # clean up the character details accordingly
            for info in self.required_information:
                if info['type'] == 'char':
                    if info['text_presentation'] is not None:
                        self.args['data'][info['key']] = info['text_presentation'](
                            self.args['data'][info['key']]
                        )

            # Server-owned values travel a TRUSTED channel (serializer.save
            # kwargs), never the request-shaped data dict. created_by is always
            # the resolved actor; a client-submitted created_by / parent FK in
            # the body is ignored (those fields are read_only on the migrated
            # write serializers) and can never override this.
            server_values = dict(self.server_values)
            if 'created_by' not in server_values and 'created_by_id' not in server_values:
                if self.user is not None:
                    server_values['created_by'] = self.user
                elif self.user_id is not None:
                    server_values['created_by_id'] = self.user_id
            record = self.serializer(data=self.data)

            if record.is_valid():
                record.save(**server_values)
                # save the attempted action to the logs
                save_action(
                    affected_model=self.model_name,
                    affected_record=str(record.data.get('id')),
                    action='create',
                    narration='Created a new record.',
                    result=ACTION_LOG_STATUSES.get('success'),
                    user_id=self.user_id,
                    username=self.username,
                    submitted_data=log_data,
                    changes=None
                )

                # TODO send a notification
                if self.msg_type:
                    try:
                        if self.user is None:
                            logger.warning(
                                "Secretary skipped notification (msg_type=%s, model=%s): "
                                "no actor user supplied. Caller must pass 'user' arg.",
                                self.msg_type, self.model_name,
                            )
                        else:
                            restaurant_id = record.data.get('restaurant')
                            make_notification_for_new_entry(
                                restaurant_id=restaurant_id,
                                user=self.user,
                                item_name=record.data.get('name'),
                                msg_type=self.msg_type,
                                record=record,
                            )
                    except Exception as error:
                        logger.error("Secretary Notification Prompt Error: %s", error)

                return {
                    'status': 200,
                    'message': self.ok_message,
                    'data': record.data
                }

            else:
                logger.error("SecretaryError-Create: %s", record.errors)
                error_message = ""
                for _, value in record.errors.items():
                    error_message += f"{', '.join(value)}\n"

                # save the attempted action to the logs
                save_action(
                    affected_model=self.model_name,
                    affected_record=None,
                    action='create',
                    narration=error_message,
                    result=ACTION_LOG_STATUSES.get('failed'),
                    user_id=self.user_id,
                    username=self.username,
                    submitted_data=log_data,
                    changes=None
                )

                return {
                    'status': 400,
                    'message': error_message
                }

    def read(self):
        """
        reads records from the database
        """
        records = self.serializer.Meta.model.objects.filter(
            **self.args.get('filter')
        )

        # save the action performed
        save_action(
            affected_model=self.model_name,
            affected_record=None,
            action='read',
            narration='Read records',
            result=ACTION_LOG_STATUSES.get('success'),
            user_id=self.user_id,
            username=self.username,
            submitted_data={},
            changes=None,
            filter_information=self.args.get('filter')
        )

        if not self.args.get('paginate'):
            data = {
                'records': self.serializer(
                    records,
                    many=True
                ).data,
                'pagination': {
                    'paginated': False,
                    'total_records': len(records),
                }
            }

            return {
                'status': 200,
                'message': self.args.get('success_message'),
                'data': data
            }

        # paginate the records
        pagination_response = DinifyPaginator({
            'request': self.args.get('request'),
            'records': records
        }).paginate()

        serialized_records = self.serializer(
            pagination_response.get('records'),
            many=True
        ).data

        # return response
        data = {
            'records': serialized_records,
            'pagination': pagination_response.get('pagination')
        }
        return {
            'status': 200,
            'message': self.ok_message,
            'data': data
        }

    def update(self):
        """
        update the record
        """
        log_data = self.formulate_log_data()

        # Scope-bound resolution: the caller MUST supply an authoritative,
        # server-built queryset defining the permitted object universe. There is
        # NO fallback to Model.objects.get — a missing queryset is a programmer
        # error and fails closed rather than resolving a row from any tenant.
        if self.instance_queryset is None:
            logger.error(
                "SecretaryError-Update: no instance_queryset supplied for %s; "
                "refusing to resolve a row from an unrestricted model manager.",
                self.model_name,
            )
            return {
                'status': 500,
                'message': 'Server misconfiguration: update scope not provided.'
            }

        with transaction.atomic():
            # get the current record — locked, and ONLY within the caller's
            # scoped queryset. A malformed / unknown / foreign id resolves to
            # nothing and returns the non-enumerating not-found posture.
            try:
                record = old_record = self.instance_queryset.select_for_update().get(
                    id=self.data.get('id')
                )
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                return {
                    'status': 404,
                    'message': 'Record not found.'
                }
            # formulate the new data to consider
            new_data = {}
            edit_considerations = self.args.get('edit_considerations')
            for item in edit_considerations:
                try:
                    key = item.get('key')
                    if key in self.data:
                        new_data[key] = self.data.get(key)
                except KeyError:
                    pass

            # attempt to format the new details accordingly.
            for info in edit_considerations:
                try:
                    if info.get('type') == 'char':
                        if info.get('text_presentation') is not None:
                            value = new_data[info['key']]
                            if value is not None:
                                # Don't apply text_presentation (e.g. str.title)
                                # to None — char fields can be cleared via null
                                # payloads now that absent vs. None is honoured.
                                new_data[info['key']] = info['text_presentation'](value)
                except KeyError:
                    pass

            # TODO determine the changes that have been made
            consider = [info.get('key') for info in edit_considerations]
            changes = determine_changes({
                'old_info': self.serializer(record, many=False).data,
                'new_info': new_data,
                'consider': consider
            })

            if len(changes) < 1:
                # Check whether a file-field operation is in play. determine_changes
                # ignores STRINGIFY_LOG_FIELDS (file fields serialise to URLs/objects
                # that don't compare cleanly), so it can never see a file change — this
                # fallback covers both an upload AND an explicit null-clear. Test
                # `key in self.data`, not `value is not None`: under the absent-vs-None
                # convention an omitted field is untouched while an explicit null clears
                # it, so a null-clear (e.g. removing a restaurant cover_photo/logo) must
                # count as a change rather than collapsing to "No changes detected".
                file_fields_present = False
                for key in STRINGIFY_LOG_FIELDS:
                    if key in self.data:
                        file_fields_present = True
                        break

                if not file_fields_present:
                    # save the action performed
                    # the log also contains the edits
                    try:
                        save_action(
                            affected_model=self.model_name,
                            affected_record=self.data.get('id'),
                            action='update',
                            narration='No changes detected.',
                            result=ACTION_LOG_STATUSES.get('failed'),
                            user_id=self.user_id,
                            username=self.username,
                            submitted_data=log_data,
                            changes=None,
                            filter_information=None
                        )
                    except Exception as error:
                        logger.error("SecretaryError-Update: %s", error)
                    return {
                        'status': 400,
                        'message': 'No changes detected'
                    }

            # update the record
            new_data['time_last_updated'] = timezone.now()
            record = self.serializer(
                record,
                data=new_data,
                partial=True
            )
            old_record = copy.deepcopy(old_record)
            if record.is_valid():
                record.save()
                # save to the log
                # construct the log data to consider
                try:
                    save_action(
                        affected_model=self.model_name,
                        affected_record=self.data.get('id'),
                        action='update',
                        narration='Updated a record',
                        result=ACTION_LOG_STATUSES.get('success'),
                        user_id=self.user_id,
                        username=self.username,
                        submitted_data=log_data,
                        changes=changes,
                    )
                except Exception as error:
                    logger.error("SecretaryError-Update: %s", error)

                return {
                    'status': 200,
                    'message': self.ok_message
                }
            else:
                logger.error("SecretaryError-Update: %s", record.errors)
                error_message = ""
                for _, value in record.errors.items():
                    error_message += f"{', '.join(value)}\n"

                try:
                    save_action(
                        affected_model=self.model_name,
                        affected_record=self.data.get('id'),
                        action='update',
                        narration=error_message,
                        result=ACTION_LOG_STATUSES.get('failed'),
                        user_id=self.user_id,
                        username=self.username,
                        submitted_data=new_data,
                        changes=changes,
                    )
                except Exception as error:
                    logger.error("SecretaryError-Update: %s", error)
                return {
                    'status': 400,
                    'message': error_message
                }

    def delete(self):
        """
        flags a record as deleted
        """
        log_data = self.formulate_log_data()
        # check if the user has provided the reason for deleting the record
        deletion_reason = self.data.get('deletion_reason')
        if deletion_reason is None:
            save_action(
                affected_model=self.model_name,
                affected_record=self.data.get('id'),
                action='delete',
                narration=MESSAGES.get('NO_DELETION_REASON'),
                result=ACTION_LOG_STATUSES.get('failed'),
                user_id=self.user_id,
                username=self.username,
                submitted_data=self.data,
                changes=None,
            )
            return {
                'status': 400,
                'message': MESSAGES.get('NO_DELETION_REASON')
            }

        # Scope-bound resolution (see update): an authoritative, server-built
        # queryset is REQUIRED — there is NO unrestricted Model.objects.get
        # fallback. A missing scope is a programmer error and fails closed.
        if self.instance_queryset is None:
            logger.error(
                "SecretaryError-Delete: no instance_queryset supplied for %s; "
                "refusing to resolve a row from an unrestricted model manager.",
                self.model_name,
            )
            return {
                'status': 500,
                'message': 'Server misconfiguration: delete scope not provided.'
            }

        with transaction.atomic():
            # lock the row within the caller's scope. A malformed / unknown /
            # foreign id resolves to nothing → non-enumerating not-found.
            try:
                record = self.instance_queryset.select_for_update().get(
                    id=self.args.get('data').get('id')
                )
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                return {
                    'status': 404,
                    'message': 'Record not found.'
                }

            if record.deleted:
                save_action(
                    affected_model=self.model_name,
                    affected_record=self.data.get('id'),
                    action='delete',
                    narration=MESSAGES.get('ALREADY_DELETED'),
                    result=ACTION_LOG_STATUSES.get('failed'),
                    user_id=self.user_id,
                    username=self.username,
                    submitted_data=log_data,
                    changes=None,
                )
                return {
                    'status': 400,
                    'message': MESSAGES.get('ALREADY_DELETED')
                }

            # Trusted soft-delete transition: the actor and server timestamp are
            # written DIRECTLY on the locked instance — never accepted as
            # serializer input (deleted / deleted_by / time_deleted / deletion_reason
            # are read_only on the migrated write serializers).
            record.deleted = True
            record.time_deleted = timezone.now()
            record.deletion_reason = deletion_reason
            if self.user is not None:
                record.deleted_by = self.user
            elif self.user_id is not None:
                record.deleted_by_id = self.user_id
            record.save(update_fields=[
                'deleted', 'time_deleted', 'deletion_reason',
                'deleted_by', 'time_last_updated',
            ])

            save_action(
                affected_model=self.model_name,
                affected_record=self.data.get('id'),
                action='delete',
                narration='Deleted a record',
                result=ACTION_LOG_STATUSES.get('success'),
                user_id=self.user_id,
                username=self.username,
                submitted_data=log_data,
                changes=None,
            )

            # vacuum deleted records — the cron job done inline
            ConVacuumDeletedRecords().vacuum()

            return {
                'status': 200,
                'message': MESSAGES.get('OK_DELETION')
            }
