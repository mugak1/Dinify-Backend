from typing import Optional
import requests
import xml.etree.ElementTree as ET
from decouple import config
from dinify_backend.mongo_db import MONGO_DB, COL_YO_RESPONSES

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
                'transaction_id': transaction_id
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
                'transaction_id': transaction_id
            },
            yo_response=yo_payment_request
        )
        print(response)
        return True

    def bank_create_verified_account(
        self,
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
        print(response)
        return True