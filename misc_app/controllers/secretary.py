"""
implementation for crud functions to the database
"""
from django.db import transaction
from django.utils import timezone
from dataclasses import dataclass
from dinify_backend.configs import IGNORE_LOG_FIELDS, STRINGIFY_LOG_FIELDS, MESSAGES, ACTION_LOG_STATUSES
from misc_app.controllers.check_required_information import check_required_information
from misc_app.controllers.paginator import DinifyPaginator
from misc_app.controllers.determine_changes import determine_changes
from misc_app.controllers.save_action_log import save_action


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
                return {
                    'status': 200,
                    'message': self.ok_message,
                    'data': record.data
                }

            else:
                print(f"SecretaryError-Create:{record.errors}")
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
            return {
                'status': 200,
                'message': self.args.get('success_message'),
                'data': {
                    'records': self.serializer(
                        records,
                        many=True
                    ).data,
                    'pagination': {
                        'paginated': False,
                        'total_records': len(records),
                    }
                }
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
        return {
            'status': 200,
            'message': self.ok_message,
            'data': {
                'records': serialized_records,
                'pagination': pagination_response.get('pagination')
            }
        }

    def update(self):
        """
        update the record
        """
        with transaction.atomic():
            # get the current record
            record = self.serializer.Meta.model.objects.get(
                id=self.data.get('id')
            )

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
                # save the action performed
                # the log also contains the edits
                save_action(
                    affected_model=self.model_name,
                    affected_record=self.data.get('id'),
                    action='update',
                    narration='No changes detected.',
                    result=ACTION_LOG_STATUSES.get('failed'),
                    user_id=self.user_id,
                    username=self.username,
                    submitted_data=new_data,
                    changes=None,
                    filter_information=None
                )
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
            if record.is_valid():
                record.save()
                # save to the log
                save_action(
                    affected_model=self.model_name,
                    affected_record=self.data.get('id'),
                    action='update',
                    narration='Updated a record',
                    result=ACTION_LOG_STATUSES.get('success'),
                    user_id=self.user_id,
                    username=self.username,
                    submitted_data=new_data,
                    changes=changes,
                )

                return {
                    'status': 200,
                    'message': self.ok_message
                }
            else:
                print(f"SecretaryError-Update:{record.errors}")
                error_message = ""
                for _, value in record.errors.items():
                    error_message += f"{', '.join(value)}\n"

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
        update_information = {
            'deleted': True,
            'time_deleted': timezone.now(),
            'deletion_reason': deletion_reason,
            'deleted_by': self.args.get('user_id')
        }
        record = serializer(
            record,
            data=update_information,
            partial=True
        )

        if record.is_valid():
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
            return {
                'status': 200,
                'message': MESSAGES.get('OK_DELETION')
            }
        else:
            print(f"SecretaryError-Delete:{record.errors}")
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
