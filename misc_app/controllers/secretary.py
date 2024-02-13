"""
implementation for crud functions to the database
"""
from django.db import transaction
from dataclasses import dataclass
from dinify_backend.configs import IGNORE_LOG_FIELDS, STRINGIFY_LOG_FIELDS
from misc_app.controllers.check_required_information import check_required_information
from misc_app.controllers.paginator import DinifyPaginator


@dataclass
class Secretary:
    """
    Class which handles general db crud operations
    """
    args: dict

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
            # check if the required information is present
            info_check = check_required_information(
                self.args['required_information'],
                self.args['data']
            )
            if not info_check['status']:
                # TODO saved the attempted action to the logs
                return {
                    'status': 400,
                    'message': info_check['message']
                }

            # clean up the character details accordingly
            for info in self.args['required_information']:
                if info['type'] == 'char':
                    if info['text_presentation'] is not None:
                        self.args['data'][info['key']] = info['text_presentation'](
                            self.args['data'][info['key']]
                        )

            affected_model = self.args['serializer'].Meta.model.__name__

            self.args['data']['created_by'] = self.args['user_id']
            record = self.args['serializer'](data=self.args['data'])

            if record.is_valid():
                record.save()
                # TODO save the attempted action to the logs
                return {
                    'status': 200,
                    'message': self.args['success_message'],
                    'data': record.data
                }

            else:
                print(f"SecretaryError-Create:{record.errors}")
                error_message = ""
                for _, value in record.errors.items():
                    error_message += f"{', '.join(value)}\n"

                # TODO save the attempted action to the logs

                return {
                    'status': 400,
                    'message': error_message
                }

    def read(self):
        """
        reads records from the database
        """
        records = self.args.get('serializer').Meta.model.objects.filter(
            **self.args.get('filter')
        )

        if not self.args.get('paginate'):
            return {
                'status': 200,
                'message': self.args.get('success_message'),
                'data': {
                    'records': self.args.get('serializer')(
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

        serialized_records = self.args.get('serializer')(
            pagination_response.get('records'),
            many=True
        ).data

        # return response
        return {
            'status': 200,
            'message': self.args.get('success_message'),
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
            data = self.args.get('data')

            # get the current record
            record = self.args.get('serializer').Meta.model.objects.get(
                id=data.get('id')
            )

            # formulate the new data to consider
            new_data = {}
            edit_considerations = self.args.get('edit_considerations')
            for item in edit_considerations:
                try:
                    key = item.get('key')
                    new_data[key] = data.get(key)
                except KeyError:
                    pass

            # attempt to format the new details accordingly.
            for info in edit_considerations:
                try:
                    if info.get('type') == 'char':
                        if info.get('text_presentation') is not None:
                            new_data[info.get('key')] = info[
                                'text_presentation'
                            ](
                                new_data[info['key']]
                            )
                except KeyError:
                    pass