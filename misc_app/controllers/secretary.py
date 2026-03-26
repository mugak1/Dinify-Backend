"""
implementation for crud functions to the database
"""
import copy
import logging
from django.db import transaction

logger = logging.getLogger(__name__)
from django.utils import timezone
from dataclasses import dataclass
from dinify_backend.configs import (
    IGNORE_LOG_FIELDS, STRINGIFY_LOG_FIELDS,
    ACTION_LOG_STATUSES
)
from dinify_backend.configss.messages import MESSAGES
from misc_app.controllers.check_required_information import check_required_information
from misc_app.controllers.notifications.msg_builder_restaurant import make_restaurant_messages
from misc_app.controllers.paginator import DinifyPaginator
from misc_app.controllers.determine_changes import determine_changes
from misc_app.controllers.save_action_log import save_action
from misc_app.management.commands.vacuum_deleted_records import ConVacuumDeletedRecords
from restaurants_app.models import Restaurant, MenuItem
from users_app.models import User
from misc_app.controllers.notifications.notification import Notification
from misc_app.controllers.con_class_utils import ConMiscUtils
from restaurants_app.serializers import SerializerAdminGetOrderReview
from restaurants_app.serializers import SerializerPutRestaurant
from restaurants_app.controllers.get_review_summary import get_review_summary


def make_notification_for_new_entry(
    restaurant_id: str,
    user: User,
    item_name: str,
    msg_type: str
):
    """
    make a notification
    """
    if restaurant_id is not None:
        restaurant_name = Restaurant.objects.values('name').get(id=restaurant_id)['name']
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
        self.fail_message = self.args.get('error_message')
        self.user_id = self.args.get('user_id')
        self.username = self.args.get('username')
        self.data = self.args.get('data')
        self.required_information = self.args.get('required_information')
        self.user = self.args.get('user')
        self.msg_type = self.args.get('msg_type')

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

            self.args['data']['created_by'] = self.user_id
            record = self.serializer(data=self.data)

            if record.is_valid():
                record.save()
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
                try:
                    restaurant_id = record.data.get('restaurant')
                    if self.msg_type in ['new-menu-section']:
                        restaurant_id = record.data['restaurant']
                    make_notification_for_new_entry(
                        restaurant_id=restaurant_id,
                        user=self.user,
                        item_name=record.data.get('name'),
                        msg_type=self.msg_type
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

        # include the summary of the reviews
        if self.serializer == SerializerAdminGetOrderReview:
            reviews_summary = get_review_summary(
                restaurant_id=self.args.get('filter').get('restaurant')
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
            if self.serializer == SerializerAdminGetOrderReview:
                data['summary'] = reviews_summary

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
        if self.serializer == SerializerAdminGetOrderReview:
            data['summary'] = reviews_summary
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

        with transaction.atomic():
            # get the current record
            record = old_record = self.serializer.Meta.model.objects.get(
                id=self.data.get('id')
            )
            # print(f"old record details: {old_record.status}")
            # formulate the new data to consider
            new_data = {}
            edit_considerations = self.args.get('edit_considerations')
            for item in edit_considerations:
                try:
                    key = item.get('key')
                    if self.data.get(key) is not None:
                        new_data[key] = self.data.get(key)
                except KeyError:
                    pass

            # attempt to format the new details accordingly.
            for info in edit_considerations:
                try:
                    if info.get('type') == 'char':
                        # check if the key has been included in the new data
                        if info.get('text_presentation') is not None:
                            new_data[info.get('key')] = info[
                                'text_presentation'
                            ](
                                new_data[info['key']]
                            )
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
                # check if any of the file fields is provided
                file_fields_present = False
                for key in STRINGIFY_LOG_FIELDS:
                    check = self.data.get(key)
                    if check is not None:
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

                self.make_notification(
                    old_record=old_record,
                    new_record=record
                )

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

        serializer = self.serializer

        # get the record and check that it has not been deleted before
        record = serializer.Meta.model.objects.get(
            id=self.args.get('data').get('id')
        )
        if record.deleted:
            save_action(
                affected_model=self.model_name,
                affected_record=self.data.get('id'),
                action='delete',
                narration=MESSAGES.get('ALREADY_DELETED'),
                result=ACTION_LOG_STATUSES.get('failed'),
                user_id=self.user_id,
                username=self.username,
                submitted_data=self.data,
                changes=None,
            )

            return {
                'status': 400,
                'message': MESSAGES.get('ALREADY_DELETED')
            }

        # flag the record as deleted
        update_information = self.data
        update_information['deleted'] = True
        update_information['time_deleted'] = timezone.now()
        update_information['deletion_reason'] = deletion_reason
        update_information['deleted_by'] = self.args.get('user_id')
        record = serializer(
            record,
            data=update_information,
            partial=True
        )

        if record.is_valid():
            logger.debug("Record deleted")
            record.save()
            save_action(
                affected_model=self.model_name,
                affected_record=self.data.get('id'),
                action='delete',
                narration='Deleted a record',
                result=ACTION_LOG_STATUSES.get('success'),
                user_id=self.user_id,
                username=self.username,
                submitted_data=self.data,
                changes=None,
            )

            # vacuum deleted records
            # this is typically doing the cron job inline
            ConVacuumDeletedRecords().vacuum()

            return {
                'status': 200,
                'message': MESSAGES.get('OK_DELETION')
            }
        else:
            logger.error("SecretaryError-Delete: %s", record.errors)
            error_message = ""
            for _, value in record.errors.items():
                error_message += f"{', '.join(value)}\n"
            save_action(
                affected_model=self.model_name,
                affected_record=self.data.get('id'),
                action='delete',
                narration=error_message,
                result=ACTION_LOG_STATUSES.get('success'),
                user_id=self.user_id,
                username=self.username,
                submitted_data=self.data,
                changes=None,
            )

            return {
                'status': 400,
                'message': error_message
            }

    def make_notification(self, old_record, new_record):
        # check if the status of the restaurant has changed
        if self.serializer is SerializerPutRestaurant:
            if old_record.status != new_record.instance.status:
                if new_record.instance.status == 'active':
                    Notification(msg_data={
                        'msg_type': 'restaurant-activated',
                        'first_name': new_record.instance.owner.first_name,
                        'restaurant_id': str(old_record.id),
                        'restaurant_name': old_record.name,
                        'user_id': str(new_record.instance.owner.id)
                    }).create_notification()

                elif new_record.instance.status == 'rejected':
                    Notification(msg_data={
                        'msg_type': 'restaurant-rejected',
                        'first_name': new_record.instance.owner.first_name,
                        'restaurant_id': str(old_record.id),
                        'restaurant_name': old_record.name,
                        'user_id': str(new_record.instance.owner.id)
                    }).create_notification()
