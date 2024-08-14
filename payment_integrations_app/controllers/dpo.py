import requests, datetime
import xml.etree.ElementTree as ET
from decouple import config
from finance_app.models import DinifyTransaction
from dataclasses import dataclass


@dataclass
class DpoIntegration:

    amount: int
    currency: str
    msisdn: str
    transaction_reference: str
    timestamp: str

    API_URL = 'https://secure.3gdirectpay.com/API/v6/'
    PAYMENT_URL = 'https://secure.3gdirectpay.com/payv3.php?ID='
    COMPANY_TOKEN = config('DPO_COMPANY_TOKEN')
    SERVICE_TYPE = config('DPO_SERVICE_TYPE')
    REDIRECT_URL = 'https://dinify-web'

    def create_token(self):
        """
        - Creates a token with DPO to enable a user make a payment for their order
        """
        print(self.timestamp)
        api3g = ET.Element('API3G')
        request = ET.SubElement(api3g, 'Request')
        request.text = 'createToken'
        company_token = ET.SubElement(api3g, 'CompanyToken')
        company_token.text = self.COMPANY_TOKEN
        # the transaction details
        transaction = ET.SubElement(api3g, 'Transaction')
        payment_amount = ET.SubElement(transaction, 'PaymentAmount')
        payment_amount.text = str(self.amount)
        payment_currency = ET.SubElement(transaction, 'PaymentCurrency')
        payment_currency.text = self.currency
        company_ref_unique = ET.SubElement(transaction, 'CompanyRefUnique')
        company_ref_unique.text = '1'  # confirm that we are not doing payments
        company_ref = ET.SubElement(transaction, 'CompanyRef')
        company_ref.text = self.transaction_reference
        redirect_url = ET.SubElement(transaction, 'RedirectURL')
        redirect_url.text = self.REDIRECT_URL
        # the service details
        services = ET.SubElement(api3g, 'Services')
        service = ET.SubElement(services, 'Service')
        service_type = ET.SubElement(service, 'ServiceType')
        service_type.text = self.SERVICE_TYPE
        service_description = ET.SubElement(service, 'ServiceDescription')
        service_description.text = 'Dinify Order Payment'
        service_date = ET.SubElement(service, 'ServiceDate')
        new_time = datetime.datetime.strptime(str(self.timestamp[:-13]), '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
        # new_time = self.timestamp
        service_date.text = new_time

        REQUEST_HEADERS = {
            'Content-Type': 'text/xml',
            'Content-transfer-encoding': 'text'
        }

        post_data = ET.tostring(api3g, xml_declaration=True, encoding='utf-8')
        dpo_token_request = requests.post(
            self.API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        response_xml_object = ET.fromstring(dpo_token_request.text)
        print('the string response from dpo is ', str(response_xml_object))

        """
        <API3G>
        <Result>000</Result>
        <ResultExplanation>Transaction created</ResultExplanation>
        <TransToken>D0F24D8E-193F-4F65-9418-7A3420FEBB48</TransToken>
        <TransRef>R21349961</TransRef>
        </API3G>
        """

        # <API3G><Result>000</Result><ResultExplanation>Transaction created</ResultExplanation><TransToken>A217F49D-2E92-4F11-8A50-CD43C9C269EC</TransToken><TransRef>R21350043</TransRef></API3G>'
        dpo_transaction_result = response_xml_object.find('Result')
        dpo_transaction_result_explanation = response_xml_object.find('ResultExplanation')
        dpo_transaction_token = response_xml_object.find('TransToken')
        dpo_transaction_ref = response_xml_object.find('TransRef')

        print(
            dpo_transaction_result.text,
            dpo_transaction_result_explanation.text,
            dpo_transaction_token.text,
            dpo_transaction_ref.text
        )

        # return None

        # update the transaction to have the new details
        txs = DinifyTransaction.objects.get(id=self.transaction_reference)
        txs.aggregator = 'DPO'
        txs.aggregator_misc_details = {
            'transaction_token': dpo_transaction_token.text,
            'transaction_reference': dpo_transaction_ref.text,
            'result': dpo_transaction_result.text,
            'result_explanation': dpo_transaction_result_explanation.text
        }
        if dpo_transaction_result.text == '000':
            txs.aggregator_reference = dpo_transaction_ref.text
        else:
            txs.transaction_status = 'Failed'
        txs.save()

        # return only the token
        if dpo_transaction_result.text == '000':
            # return dpo_transaction_token.text
            return f'{self.PAYMENT_URL}{dpo_transaction_token.text}'
        return None
