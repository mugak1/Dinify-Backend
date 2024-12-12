from urllib import response
import requests
import datetime
import xml.etree.ElementTree as ET
from decouple import config
from finance_app.models import DinifyTransaction
from dataclasses import dataclass
from dinify_backend.configss.string_definitions import (
    TransactionStatus_Failed,
    TransactionStatus_Success,
    TransactionStatus_Pending,
    Aggregator_DPO
)
from dinify_backend.mongo_db import (
    MONGO_DB,
    COL_DPO_TOKENS,
    COL_DPO_TOKEN_VERIFICATION,
    COL_DPO_RESPONSES
)
from misc_app.controllers.save_to_mongo import save_to_mongodb
from finance_app.controllers.process_payment_feedback import process_payment_feedback


@dataclass
class DpoIntegration:

    amount: int
    currency: str
    msisdn: str
    transaction_reference: str
    timestamp: str
    dpo_transaction_token: str

    API_URL = 'https://secure.3gdirectpay.com/API/v6/'
    PAYMENT_URL = 'https://secure.3gdirectpay.com/payv3.php?ID='
    COMPANY_TOKEN = config('DPO_COMPANY_TOKEN')
    SERVICE_TYPE = config('DPO_SERVICE_TYPE')
    REDIRECT_URL = 'https://dinify-web'

    def interprete_response(self, request_type: str, request_body: dict, dpo_response: str):
        response_xml_object = ET.fromstring(dpo_response.text)

        dpo_response_dict = {}
        for elem in response_xml_object.iter():
            dpo_response_dict[elem.tag] = elem.text

        MONGO_DB[COL_DPO_RESPONSES].insert_one({
            'request_type': request_type,
            'request_body': request_body,
            'response_string': dpo_response.text,
            'response_dict': dpo_response_dict
        })
        return dpo_response_dict

    def create_token(self):
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

        response = self.interprete_response(
            request_type='create_token',
            request_body={
                'amount': self.amount,
                'currency': self.currency,
                'transaction_reference': self.transaction_reference
            },
            dpo_response=dpo_token_request
        )

        # return only the token
        if response.get('Result') == '000':
            return f"{self.PAYMENT_URL}{response.get('TransToken')}"
        return None

    def create_token0(self):
        """
        - Creates a token with DPO to enable a user make a payment for their order
        """
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

        # update the transaction to have the new details
        dpo_response = {
            'transaction_result': dpo_transaction_result.text,
            'transaction_result_explanation': dpo_transaction_result_explanation.text,
            'transaction_token': dpo_transaction_token.text,
            'transaction_ref': dpo_transaction_ref.text
        }
        txs = DinifyTransaction.objects.get(id=self.transaction_reference)
        txs.aggregator = Aggregator_DPO
        txs.aggregator_misc_details = dpo_response

        if dpo_transaction_result.text == '000':
            txs.aggregator_reference = dpo_transaction_ref.text
        else:
            txs.transaction_status = TransactionStatus_Failed
        txs.save()

        save_to_mongodb(
            collection=COL_DPO_TOKENS,
            data=dpo_response
        )

        # return only the token
        if dpo_transaction_result.text == '000':
            # return dpo_transaction_token.text
            return f'{self.PAYMENT_URL}{dpo_transaction_token.text}'
        return None

    def verify_token(self):
        # <?xml version="1.0" encoding="utf-8"?>
        # <API3G>
        # <CompanyToken>57466282-EBD7-4ED5-B699-8659330A6996</CompanyToken>
        # <Request>verifyToken</Request>
        # <TransactionToken>72983CAC-5DB1-4C7F-BD88-352066B71592</TransactionToken>
        # </API3G>
        api3g = ET.Element('API3G')
        company_token = ET.SubElement(api3g, 'CompanyToken')
        company_token.text = self.COMPANY_TOKEN
        request = ET.SubElement(api3g, 'Request')
        request.text = 'verifyToken'
        transaction_token = ET.SubElement(api3g, 'TransactionToken')
        transaction_token.text = self.dpo_transaction_token

        REQUEST_HEADERS = {
            'Content-Type': 'text/xml',
            'Content-transfer-encoding': 'text'
        }

        post_data = ET.tostring(api3g, xml_declaration=True, encoding='utf-8')
        dpo_response = requests.post(
            self.API_URL,
            data=post_data,
            headers=REQUEST_HEADERS
        )
        # print(dpo_response.text)
        response_xml_object = ET.fromstring(dpo_response.text)

        # <?xml version="1.0" encoding="utf-8"?>
        # <API3G>
        # <Result>000</Result>
        # <ResultExplanation>Transaction paid</ResultExplanation>
        # <CustomerName >John Doe</CustomerName >
        # <CustomerCredit>4432</CustomerCredit>
        # <TransactionApproval>938204312</TransactionApproval>
        # <TransactionCurrency>USD</TransactionCurrency>
        # <TransactionAmount>950.00</TransactionAmount>
        # <FraudAlert>000</FraudAlert>
        # <FraudExplnation>No Fraud detected</FraudExplnation>
        # <TransactionNetAmount>945</TransactionNetAmount>
        # <TransactionSettlementDate>2013/12/31</TransactionSettlementDate>
        # <TransactionRollingReserveAmount>5</TransactionRollingReserveAmount>
        # <TransactionRollingReserveDate>2014/12/31</TransactionRollingReserveDate>
        # <CustomerPhone>254123456789</CustomerPhone>
        # <CustomerCountry>KE</CustomerCountry>
        # <CustomerAddress>Stranfe blvd.</CustomerAddress>
        # <CustomerCity>Nairobi</CustomerCity>
        # <CustomerZip>AH1</CustomerZip>
        # <MobilePaymentRequest>Sent</MobilePaymentRequest>
        # <AccRef>ABC123REF</AccRef>
        # </API3G>
        dpo_transaction_result = response_xml_object.find('Result')
        dpo_transaction_result_explanation = response_xml_object.find('ResultExplanation')
        dpo_transaction_customer_name = response_xml_object.find('CustomerName')
        dpo_customer_credit = response_xml_object.find('CustomerCredit')
        dpo_transaction_approval = response_xml_object.find('TransactionApproval')
        dpo_transaction_currency = response_xml_object.find('TransactionCurrency')
        dpo_transaction_amount = response_xml_object.find('TransactionAmount')
        dpo_fraud_alert = response_xml_object.find('FraudAlert')
        dpo_fraud_explanation = response_xml_object.find('FraudExplnation')
        dpo_transaction_net_amount = response_xml_object.find('TransactionNetAmount')
        dpo_transaction_settlement_date = response_xml_object.find('TransactionSettlementDate')
        dpo_transaction_rolling_reserve_amount = response_xml_object.find('TransactionRollingReserveAmount')  # noqa
        dpo_transaction_rolling_reserve_date = response_xml_object.find('TransactionRollingReserveDate')  # noqa
        dpo_customer_phone = response_xml_object.find('CustomerPhone')
        dpo_customer_country = response_xml_object.find('CustomerCountry')
        dpo_customer_address = response_xml_object.find('CustomerAddress')
        dpo_customer_city = response_xml_object.find('CustomerCity')
        dpo_customer_zip = response_xml_object.find('CustomerZip')
        dpo_mobile_payment_request = response_xml_object.find('MobilePaymentRequest')
        dpo_acc_ref = response_xml_object.find('AccRef')
        misc_details = {
            'dpo_transaction_token': self.dpo_transaction_token,
            'transaction_result': dpo_transaction_result.text if dpo_transaction_result is not None else None,  # noqa
            'transaction_result_explanation': dpo_transaction_result_explanation.text if dpo_transaction_result_explanation is not None else None,  # noqa
            'customer_name': dpo_transaction_customer_name.text if dpo_transaction_customer_name is not None else None,  # noqa
            'customer_credit': dpo_customer_credit.text if dpo_customer_credit is not None else None,  # noqa
            'transaction_approval': dpo_transaction_approval.text if dpo_transaction_approval is not None else None,  # noqa
            'transaction_currency': dpo_transaction_currency.text if dpo_transaction_currency is not None else None,  # noqa
            'transaction_amount': dpo_transaction_amount.text if dpo_transaction_amount is not None else None,  # noqa
            'fraud_alert': dpo_fraud_alert.text if dpo_fraud_alert is not None else None,  # noqa
            'fraud_explanation': dpo_fraud_explanation.text if dpo_fraud_explanation is not None else None,  # noqa
            'transaction_net_amount': dpo_transaction_net_amount.text if dpo_transaction_net_amount is not None else None,  # noqa
            'transaction_settlement_date': dpo_transaction_settlement_date.text if dpo_transaction_settlement_date is not None else None,  # noqa
            'transaction_rolling_reserve_amount': dpo_transaction_rolling_reserve_amount.text if dpo_transaction_rolling_reserve_amount is not None else None,  # noqa
            'transaction_rolling_reserve_date': dpo_transaction_rolling_reserve_date.text if dpo_transaction_rolling_reserve_date is not None else None,  # noqa
            'customer_phone': dpo_customer_phone.text if dpo_customer_phone is not None else None,  # noqa
            'customer_country': dpo_customer_country.text if dpo_customer_country is not None else None,  # noqa
            'customer_address': dpo_customer_address.text if dpo_customer_address is not None else None,  # noqa
            'customer_city': dpo_customer_city.text if dpo_customer_city is not None else None,  # noqa
            'customer_zip': dpo_customer_zip.text if dpo_customer_zip is not None else None,  # noqa
            'mobile_payment_request': dpo_mobile_payment_request.text if dpo_mobile_payment_request is not None else None,  # noqa
            'acc_ref': dpo_acc_ref.text if dpo_acc_ref is not None else None
        }

        save_to_mongodb(
            collection=COL_DPO_TOKEN_VERIFICATION,
            data=misc_details
        )

        transaction_status = TransactionStatus_Pending
        if dpo_transaction_result.text in ['000']:
            transaction_status = TransactionStatus_Success
        elif dpo_transaction_result.text in ['901', '902', '903', '904', '950']:
            transaction_status = TransactionStatus_Failed

        if transaction_status in [
            TransactionStatus_Success,
            TransactionStatus_Failed
        ]:
            process_payment_feedback(
                transaction_id=self.transaction_reference,
                aggregator=Aggregator_DPO,
                aggregator_reference=dpo_transaction_approval.text,
                aggregator_status=dpo_transaction_result.text,
                status=transaction_status
            )

        return {
            'status': 200,
            'message': 'Token verified',
        }


# 000	Transaction Paid
# 001	Authorized
# 002	Transaction overpaid/underpaid
# 003	Pending Bank
# 005	Queued Authorization
# 007	Pending Split Payment (Part Payment Transactions not fully paid)
# 801	Request missing company token
# 802	Company token does not exist
# 803	No request or error in Request type name
# 804	Error in XML
# 900	Transaction not paid yet
# 901	Transaction declined
# 902	Data mismatch in one of the fields - field (explanation)
# 903	The transaction passed the Payment Time Limit
# 904	Transaction cancelled
# 950	Request missing transaction level mandatory fields – field (explanation)