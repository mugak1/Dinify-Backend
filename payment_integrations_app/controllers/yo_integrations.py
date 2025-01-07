from re import A
from typing import Optional
import requests
import xml.etree.ElementTree as ET
from decouple import config
from bson import ObjectId
from django.db import transaction
from dinify_backend.mongo_db import MONGO_DB, COL_YO_RESPONSES
from finance_app.endpoints import bank_account
from finance_app.models import BankAccountRecord, DinifyTransaction
from misc_app.controllers.flag_doc_as_processed import flag_doc_as_processed
from dinify_backend.configss.string_definitions import (
    ProcessingStatus_Pending,
    ProcessingStatus_Failed,
    ProcessingStatus_Confirmed,
    Aggregator_Yo
)


# API_URL = 'https://paymentsapi2.yo.co.ug/ybs/task.php'
API_URL = 'https://sandbox.yo.co.ug/services/yopaymentsdev/task.php'

REQUEST_HEADERS = {
    'Content-Type': 'text/xml',
    'Content-transfer-encoding': 'text'
}


class YoIntegration:
    def __init__(self):
        self.YO_USERNAME = config('YO_API_USERNAME')
        self.YO_PASSWORD = config('YO_API_PASSWORD')

        self.YO_SMS_ACCOUNT_NO = config('YO_SMS_ACCOUNT_NO')
        self.YO_SMS_PASSWORD = config('YO_SMS_PASSWORD')

    def interprete_response(self, request_type: str, request_body: dict, yo_response: str) -> dict:
        response_xml_object = ET.fromstring(yo_response.text)
        yo_response_dict = None
        try:
            response_element = response_xml_object.find('Response')
            if response_element is not None:
                yo_response_dict = {child.tag: child.text for child in response_element}
        except Exception as error:
            print(f"\nError interpreting Yo Response: {error}\n")

        MONGO_DB[COL_YO_RESPONSES].insert_one({
            'request_type': request_type,
            'request_body': request_body,
            'response_string': yo_response.text,
            'response_dict': yo_response_dict
        })

        return yo_response_dict

    def momo_collect(self, transaction_amount: int, msisdn: str, transaction_id: str) -> bool:
        auto_create = ET.Element('AutoCreate')
        request = ET.SubElement(auto_create, 'Request')
        api_username = ET.SubElement(request, 'APIUsername')
        api_username.text = self.YO_USERNAME
        api_password = ET.SubElement(request, 'APIPassword')
        api_password.text = self.YO_PASSWORD
        method = ET.SubElement(request, 'Method')
        method.text = 'acdepositfunds'
        non_blocking = ET.SubElement(request, 'NonBlocking')
        non_blocking.text = 'TRUE'
        amount = ET.SubElement(request, 'Amount')
        amount.text = str(transaction_amount)
        account = ET.SubElement(request, 'Account')
        account.text = msisdn
        narrative = ET.SubElement(request, 'Narrative')
        narrative.text = 'Dinify Order Payment'
        external_reference = ET.SubElement(request, 'ExternalReference')
        external_reference.text = transaction_id
        provider_reference_text = ET.SubElement(request, 'ProviderReferenceText')
        provider_reference_text.text = 'Dinify Order Payment'

        # print(f"{self.YO_USERNAME} {self.YO_PASSWORD}")
        # return True
        post_data = ET.tostring(auto_create, xml_declaration=True, encoding='utf-8')

        yo_payment_request = requests.post(
            API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        response = self.interprete_response(
            request_type='momo_collect',
            request_body={
                'amount': transaction_amount,
                'msisdn': msisdn,
                'transaction_id': str(transaction_id)
            },
            yo_response=yo_payment_request
        )
        print(response)
        return True

    def momo_check_transaction(self, yo_transaction_reference: str) -> bool:
        auto_create = ET.Element('AutoCreate')
        request = ET.SubElement(auto_create, 'Request')
        api_username = ET.SubElement(request, 'APIUsername')
        api_username.text = self.YO_USERNAME
        api_password = ET.SubElement(request, 'APIPassword')
        api_password.text = self.YO_PASSWORD
        method = ET.SubElement(request, 'Method')
        method.text = 'actransactioncheckstatus'
        transaction_reference = ET.SubElement(request, 'TransactionReference')
        transaction_reference.text = yo_transaction_reference

        post_data = ET.tostring(auto_create, xml_declaration=True, encoding='utf-8')
        yo_request = requests.post(
            API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        response = self.interprete_response(
            request_type='momo_check_transaction',
            request_body={'yo_transaction_reference': yo_transaction_reference},
            yo_response=yo_request
        )
        print(response)
        return True

    def momo_disburse(self, transaction_amount: int, msisdn: str, transaction_id: str) -> bool:
        auto_create = ET.Element('AutoCreate')
        request = ET.SubElement(auto_create, 'Request')
        api_username = ET.SubElement(request, 'APIUsername')
        api_username.text = self.YO_USERNAME
        api_password = ET.SubElement(request, 'APIPassword')
        api_password.text = self.YO_PASSWORD
        method = ET.SubElement(request, 'Method')
        method.text = 'acwithdrawfunds'
        non_blocking = ET.SubElement(request, 'NonBlocking')
        non_blocking.text = 'TRUE'
        amount = ET.SubElement(request, 'Amount')
        amount.text = str(transaction_amount)
        account = ET.SubElement(request, 'Account')
        account.text = msisdn
        narrative = ET.SubElement(request, 'Narrative')
        narrative.text = 'Dinify Disbursement'
        external_reference = ET.SubElement(request, 'ExternalReference')
        external_reference.text = transaction_id
        provider_reference_text = ET.SubElement(request, 'ProviderReferenceText')
        provider_reference_text.text = 'Dinify Disbursement'

        post_data = ET.tostring(auto_create, xml_declaration=True, encoding='utf-8')

        yo_payment_request = requests.post(
            API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        response = self.interprete_response(
            request_type='momo_disburse',
            request_body={
                'amount': transaction_amount,
                'msisdn': msisdn,
                'transaction_id': str(transaction_id)
            },
            yo_response=yo_payment_request
        )
        print(response)
        return True

    def bank_create_verified_account(
        self,
        arg_account_id: str,
        arg_account_name: str,
        arg_account_number: str,
        arg_bank_name: str,
        arg_address_line1: str,
        arg_address_line2: str,
        arg_city: str,
        arg_country: str,
        arg_state: Optional[str] = None,
        arg_swift_code: Optional[str] = None,
        arg_sort_code: Optional[str] = None,
        arg_aba_number: Optional[str] = None,
        arg_routing_number: Optional[str] = None
    ) -> bool:
        auto_create = ET.Element('AutoCreate')
        request = ET.SubElement(auto_create, 'Request')
        api_username = ET.SubElement(request, 'APIUsername')
        api_username.text = self.YO_USERNAME
        api_password = ET.SubElement(request, 'APIPassword')
        api_password.text = self.YO_PASSWORD
        method = ET.SubElement(request, 'Method')
        method.text = 'accreateverifiedbankaccount'
        account_name = ET.SubElement(request, 'AccountName')
        account_name.text = arg_account_name
        account_number = ET.SubElement(request, 'AccountNumber')
        account_number.text = arg_account_number

        bank_information = ET.SubElement(request, 'BankInformation')
        name = ET.SubElement(bank_information, 'Name')
        name.text = arg_bank_name
        street_address_line1 = ET.SubElement(bank_information, 'StreetAddressLine1')
        street_address_line1.text = arg_address_line1
        street_address_line2 = ET.SubElement(bank_information, 'StreetAddressLine2')
        street_address_line2.text = arg_address_line2
        bank_city = ET.SubElement(bank_information, 'City')
        bank_city.text = arg_city
        bank_state = ET.SubElement(bank_information, 'State')
        bank_state.text = arg_state
        bank_country = ET.SubElement(bank_information, 'Country')
        bank_country.text = arg_country
        if arg_swift_code is not None:
            swift_code = ET.SubElement(bank_information, 'SwiftCode')
            swift_code.text = arg_swift_code
        if arg_sort_code is not None:
            sort_code = ET.SubElement(bank_information, 'SortCode')
            sort_code.text = arg_sort_code
        if arg_aba_number is not None:
            aba_number = ET.SubElement(bank_information, 'ABANumber')
            aba_number.text = arg_aba_number
        if arg_routing_number is not None:
            routing_number = ET.SubElement(bank_information, 'RoutingNumber')
            routing_number.text = arg_routing_number

        post_data = ET.tostring(auto_create, xml_declaration=True, encoding='utf-8')
        yo_request = requests.post(
            API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        response = self.interprete_response(
            request_type='bank_create_verified_account',
            request_body={
                'account_id': arg_account_id,
                'account_name': arg_account_name,
                'account_number': arg_account_number,
                'bank_name': arg_bank_name,
                'address_line1': arg_address_line1,
                'address_line2': arg_address_line2,
                'city': arg_city,
                'country': arg_country,
                'state': arg_state,
                'swift_code': arg_swift_code,
                'sort_code': arg_sort_code,
                'aba_number': arg_aba_number,
                'routing_number': arg_routing_number
            },
            yo_response=yo_request
        )
        # update the bank account record with the yo reference
        try:
            if response.get('Status') == 'OK':
                bank_account_record = BankAccountRecord.objects.get(id=arg_account_id)
                bank_account_record.yo_reference = response.get('ApiBankIdentifier')
                bank_account_record.save()
        except Exception as error:
            print(f"\nError updating bank account record: {error}\n")
        return True

    def bank_disburse(
        self,
        arg_transaction_id: str,
        arg_account_number: str,
        arg_account_identifier: str,
        arg_amount: int,
        arg_transfer_type: str
    ) -> bool:
        auto_create = ET.Element('AutoCreate')
        request = ET.SubElement(auto_create, 'Request')
        api_username = ET.SubElement(request, 'APIUsername')
        api_username.text = self.YO_USERNAME
        api_password = ET.SubElement(request, 'APIPassword')
        api_password.text = self.YO_PASSWORD
        method = ET.SubElement(request, 'Method')
        method.text = 'acwithdrawfundstobank'
        amount = ET.SubElement(request, 'Amount')
        amount.text = str(arg_amount)
        currency_code = ET.SubElement(request, 'CurrencyCode')
        currency_code.text = 'UGX'
        bank_account_name = ET.SubElement(request, 'BankAccountName')
        bank_account_name.text = 'ESAU LWANGA'
        bank_account_number = ET.SubElement(request, 'BankAccountNumber')
        bank_account_number.text = arg_account_number
        bank_account_identifier = ET.SubElement(request, 'BankAccountIdentifier')
        bank_account_identifier.text = arg_account_identifier
        transfer_transaction_type = ET.SubElement(request, 'TransferTransactionType')
        transfer_transaction_type.text = arg_transfer_type
        private_transaction_reference = ET.SubElement(request, 'PrivateTransactionReference')
        private_transaction_reference.text = arg_transaction_id

        post_data = ET.tostring(auto_create, xml_declaration=True, encoding='utf-8')
        yo_request = requests.post(
            API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        response = self.interprete_response(
            request_type='bank_disburse',
            request_body={
                'transaction_id': arg_transaction_id,
                'account_number': arg_account_number,
                'account_identifier': arg_account_identifier,
                'amount': arg_amount,
                'transfer_type': arg_transfer_type
            },
            yo_response=yo_request
        )
        print(response)
        return True

    def bank_check_disbursement_status(self, arg_settlement_id: str) -> bool:
        auto_create = ET.Element('AutoCreate')
        request = ET.SubElement(auto_create, 'Request')
        api_username = ET.SubElement(request, 'APIUsername')
        api_username.text = self.YO_USERNAME
        api_password = ET.SubElement(request, 'APIPassword')
        api_password.text = self.YO_PASSWORD
        method = ET.SubElement(request, 'Method')
        method.text = 'accheckbanksettlementstatus'
        settlement_transaction_identifier = ET.SubElement(request, 'SettlementTransactionIdentifier')
        settlement_transaction_identifier.text = arg_settlement_id

        post_data = ET.tostring(auto_create, xml_declaration=True, encoding='utf-8')
        yo_request = requests.post(
            API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        response = self.interprete_response(
            request_type='bank_check_disbursement_status',
            request_body={'settlement_id': arg_settlement_id},
            yo_response=yo_request
        )
        print(response)
        return True

    def send_sms(self, message: str, to: str):
        yo_request = f"http://smgw1.yo.co.ug:9100/sendsms?ybsacctno={self.YO_SMS_ACCOUNT_NO}&password={self.YO_SMS_PASSWORD}&origin=Dinify&sms_content={message}&destinations={to}&nostore=0"  # noqa
        requests.get(yo_request)
        return True

    def process_yo_response(self, response_id):
        yo_response = MONGO_DB[COL_YO_RESPONSES].find_one({'_id': ObjectId(response_id)})
        # skip if there is no response_dict
        print(f"\nProcessing Yo Response: {response_id} : {type(yo_response.get('response_dict'))}\n")

        if yo_response.get('response_dict') is None:
            print('skipping NONE response')
            flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
            return

        request_type = yo_response.get('request_type')
        print(f"\nRequest Type: {request_type}\n")

        if request_type == 'momo_collect':
            request_body = yo_response.get('request_body')
            response_dict = yo_response.get('response_dict')
            transaction_id = request_body.get('transaction_id')

            try:
                with transaction.atomic():
                    txs = DinifyTransaction.objects.select_for_update().get(id=transaction_id)
                    txs.aggregator = Aggregator_Yo

                    if response_dict.get('Status') == 'ERROR':
                        txs.processing_status = ProcessingStatus_Failed
                    elif response_dict.get('Status') == 'OK':
                        txs.aggregator_status = response_dict.get('TransactionStatus')
                        txs.aggregator_reference = response_dict.get('TransactionReference')
                    txs.save()
            except DinifyTransaction.DoesNotExist:
                pass
            flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)

        elif request_type == 'momo_check_transaction':
            print('processing transaction status check...')
            request_body = yo_response.get('request_body')
            response_dict = yo_response.get('response_dict')
            aggregator_reference = request_body.get('yo_transaction_reference')
            with transaction.atomic():
                txs_record = None
                try:
                    txs_record = DinifyTransaction.objects.select_for_update().get(
                        aggregator=Aggregator_Yo,
                        aggregator_reference=aggregator_reference
                    )
                except DinifyTransaction.DoesNotExist:
                    print('no yo transaction found')
                    pass

                if txs_record is None:
                    flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
                    return

                aggregator_status = response_dict.get('TransactionStatus')
                print(f"\nAggregator Status: {aggregator_status}\n")
                if aggregator_status is None:
                    flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
                    return

                if aggregator_status == 'SUCCEEDED':
                    if txs_record.processing_status != ProcessingStatus_Pending:
                        flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
                        return
                    txs_record.processing_status = ProcessingStatus_Confirmed
                    txs_record.aggregator_status = aggregator_status
                    txs_record.save()
                    print('transaction updated')
                else:
                    return

                flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
        else:
            return
