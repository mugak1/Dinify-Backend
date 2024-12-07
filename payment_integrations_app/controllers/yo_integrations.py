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

    def interprete_response(
        self,
        request_type: str,
        request_body: dict,
        yo_response: str
    ) -> dict:
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

    def momo_collect(
        self,
        transaction_amount: int,
        msisdn: str,
        transaction_id: str
    ) -> bool:
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

    def momo_check_transaction(
        self,
        yo_transaction_reference: str
    ) -> bool:
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
