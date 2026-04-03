import logging
import requests
from bson import ObjectId
from datetime import datetime
from django.db import transaction
import xml.etree.ElementTree as ET
from decouple import config
from finance_app.models import DinifyTransaction
from dataclasses import dataclass
from dinify_backend.configss.string_definitions import (
    TransactionStatus_Failed,
    TransactionStatus_Success,
    TransactionStatus_Pending,
    Aggregator_DPO,
    ProcessingStatus_Failed,
    ProcessingStatus_Confirmed
)
from dinify_backend.mongo_db import (
    MONGO_DB,
    COL_DPO_TOKENS,
    COL_DPO_TOKEN_VERIFICATION,
    COL_DPO_RESPONSES
)
from misc_app.controllers.save_to_mongo import save_to_mongodb
from finance_app.controllers.process_payment_feedback import process_payment_feedback
from misc_app.controllers.flag_doc_as_processed import flag_doc_as_processed

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds

REQUEST_HEADERS = {
    'Content-Type': 'text/xml',
    'Content-transfer-encoding': 'text'
}


@dataclass
class DpoIntegration:
    API_URL: str = 'https://secure.3gdirectpay.com/API/v6/'
    PAYMENT_URL: str = 'https://secure.3gdirectpay.com/payv3.php?ID='
    REDIRECT_URL: str = 'https://dinify-web'

    def __post_init__(self):
        self.COMPANY_TOKEN = config('DPO_COMPANY_TOKEN')
        self.SERVICE_TYPE = config('DPO_SERVICE_TYPE')

    def interprete_response(self, request_type: str, request_body: dict, dpo_response: str):
        try:
            response_xml_object = ET.fromstring(dpo_response.text)
        except ET.ParseError as exc:
            logger.error("DPO XML parse error for %s: %s", request_type, exc)
            return {}

        dpo_response_dict = {}
        for elem in response_xml_object.iter():
            dpo_response_dict[elem.tag] = elem.text

        try:
            MONGO_DB[COL_DPO_RESPONSES].insert_one({
                'request_type': request_type,
                'request_body': request_body,
                'response_string': dpo_response.text,
                'response_dict': dpo_response_dict
            })
        except Exception as e:
            logger.error("Failed to save DPO response to MongoDB: %s", e)
        return dpo_response_dict

    def create_token(self, amount: int, currency: str, transaction_reference: str, timestamp: datetime) -> str:  # noqa
        api3g = ET.Element('API3G')
        request = ET.SubElement(api3g, 'Request')
        request.text = 'createToken'
        company_token = ET.SubElement(api3g, 'CompanyToken')
        company_token.text = self.COMPANY_TOKEN
        # the transaction details
        transaction = ET.SubElement(api3g, 'Transaction')
        payment_amount = ET.SubElement(transaction, 'PaymentAmount')
        payment_amount.text = str(amount)
        payment_currency = ET.SubElement(transaction, 'PaymentCurrency')
        payment_currency.text = currency
        company_ref_unique = ET.SubElement(transaction, 'CompanyRefUnique')
        company_ref_unique.text = '1'  # confirm that we are not doing payments
        company_ref = ET.SubElement(transaction, 'CompanyRef')
        company_ref.text = transaction_reference
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
        new_time = datetime.strptime(str(timestamp[:-13]), '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
        service_date.text = new_time

        post_data = ET.tostring(api3g, xml_declaration=True, encoding='utf-8')
        try:
            dpo_token_request = requests.post(
                self.API_URL,
                data=post_data,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("DPO create_token request failed: %s", exc)
            return None

        response = self.interprete_response(
            request_type='create_token',
            request_body={
                'amount': amount,
                'currency': currency,
                'transaction_reference': str(transaction_reference)
            },
            dpo_response=dpo_token_request
        )

        # return only the token
        if response.get('Result') == '000':
            return f"{self.PAYMENT_URL}{response.get('TransToken')}"
        return None

    def verify_token(self, transaction_reference: str, dpo_token: str):
        api3g = ET.Element('API3G')
        company_token = ET.SubElement(api3g, 'CompanyToken')
        company_token.text = self.COMPANY_TOKEN
        request = ET.SubElement(api3g, 'Request')
        request.text = 'verifyToken'
        transaction_token = ET.SubElement(api3g, 'TransactionToken')
        transaction_token.text = dpo_token

        post_data = ET.tostring(api3g, xml_declaration=True, encoding='utf-8')
        try:
            dpo_response = requests.post(
                self.API_URL,
                data=post_data,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("DPO verify_token request failed: %s", exc)
            return False

        self.interprete_response(
            request_type='verify_token',
            request_body={
                'transaction_reference': str(transaction_reference),
                'dpo_token': dpo_token
            },
            dpo_response=dpo_response
        )
        return True

    def process_response(self, response_id: str):
        try:
            dpo_response = MONGO_DB[COL_DPO_RESPONSES].find_one({'_id': ObjectId(response_id)})
        except Exception as e:
            logger.error("Failed to read DPO response %s from MongoDB: %s", response_id, e)
            return
        if dpo_response.get('response_dict') is None:
            flag_doc_as_processed(
                collection_name=COL_DPO_RESPONSES,
                doc_id=response_id
            )
            return

        request_body = dpo_response.get('request_body')
        response_dict = dpo_response.get('response_dict')
        transaction_id = request_body.get('transaction_reference')

        if dpo_response.get('request_type') == 'create_token':
            with transaction.atomic():
                try:
                    txs_record = DinifyTransaction.objects.select_for_update().get(id=transaction_id)  # noqa
                    txs_record.aggregator = Aggregator_DPO
                    txs_record.aggregator_status = response_dict.get('Result')

                    if response_dict.get('Result') == '000':
                        txs_record.aggregator_reference = response_dict.get('TransToken')
                        txs_record.save()
                    else:
                        txs_record = DinifyTransaction.objects.select_for_update().get(id=transaction_id)  # noqa
                        txs_record.processing_status = ProcessingStatus_Failed
                        txs_record.save()
                except DinifyTransaction.DoesNotExist:
                    pass
            flag_doc_as_processed(collection_name=COL_DPO_RESPONSES, doc_id=response_id)

        elif dpo_response.get('request_type') == 'verify_token':
            with transaction.atomic():
                txs_record = None
                try:
                    txs_record = DinifyTransaction.objects.select_for_update().get(id=transaction_id)  # noqa
                except DinifyTransaction.DoesNotExist:
                    pass

                if txs_record is None:
                    flag_doc_as_processed(collection_name=COL_DPO_RESPONSES, doc_id=response_id)
                    return

                txs_record.aggregator_status = response_dict.get('Result')
                if response_dict.get('Result') == '000':
                    txs_record.processing_status = ProcessingStatus_Confirmed
                elif response_dict.get('Result') in ['901', '902', '903', '904', '950']:
                    txs_record.processing_status = ProcessingStatus_Failed
                txs_record.save()
            flag_doc_as_processed(collection_name=COL_DPO_RESPONSES, doc_id=response_id)
        else:
            return

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
