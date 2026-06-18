import logging
import threading
import requests
import xml.etree.ElementTree as ET
from decouple import config
from bson import ObjectId
from django.db import transaction
from dinify_backend.mongo_db import MONGO_DB, COL_YO_RESPONSES
from finance_app.endpoints import bank_account
from finance_app.models import DinifyTransaction
from misc_app.controllers.flag_doc_as_processed import flag_doc_as_processed
from dinify_backend.configss.string_definitions import (
    ProcessingStatus_Pending,
    ProcessingStatus_Failed,
    ProcessingStatus_Confirmed,
    Aggregator_Yo
)

logger = logging.getLogger(__name__)

# API_URL = 'https://paymentsapi2.yo.co.ug/ybs/task.php'
API_URL = 'https://sandbox.yo.co.ug/services/yopaymentsdev/task.php'

REQUEST_HEADERS = {
    'Content-Type': 'text/xml',
    'Content-transfer-encoding': 'text'
}

REQUEST_TIMEOUT = 10  # seconds


class YoIntegration:
    def __init__(self):
        self.YO_USERNAME = config('YO_API_USERNAME')
        self.YO_PASSWORD = config('YO_API_PASSWORD')

        self.YO_SMS_ACCOUNT_NO = config('YO_SMS_ACCOUNT_NO')
        self.YO_SMS_PASSWORD = config('YO_SMS_PASSWORD')

    def interprete_response(self, request_type: str, request_body: dict, yo_response: str) -> dict:
        try:
            response_xml_object = ET.fromstring(yo_response.text)
        except ET.ParseError as exc:
            logger.error("Yo XML parse error for %s: %s", request_type, exc)

            def _write_yo_error_response():
                try:
                    MONGO_DB[COL_YO_RESPONSES].insert_one({
                        'request_type': request_type,
                        'request_body': request_body,
                        'response_string': yo_response.text,
                        'response_dict': None
                    })
                except Exception as e:
                    logger.error("Failed to save Yo error response to MongoDB: %s", e)

            threading.Thread(target=_write_yo_error_response, daemon=True).start()
            return None

        yo_response_dict = None
        try:
            response_element = response_xml_object.find('Response')
            if response_element is not None:
                yo_response_dict = {child.tag: child.text for child in response_element}
        except Exception as error:
            logger.error("Error interpreting Yo Response: %s", error)

        # Fire-and-forget: archival-only, caller uses yo_response_dict
        def _write_yo_response():
            try:
                MONGO_DB[COL_YO_RESPONSES].insert_one({
                    'request_type': request_type,
                    'request_body': request_body,
                    'response_string': yo_response.text,
                    'response_dict': yo_response_dict
                })
            except Exception as e:
                logger.error("Failed to save Yo response to MongoDB: %s", e)

        threading.Thread(target=_write_yo_response, daemon=True).start()
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

        post_data = ET.tostring(auto_create, xml_declaration=True, encoding='utf-8')

        try:
            yo_payment_request = requests.post(
                API_URL,
                data=post_data,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("Yo momo_collect request failed: %s", exc)
            return False
        response = self.interprete_response(
            request_type='momo_collect',
            request_body={
                'amount': transaction_amount,
                'msisdn': msisdn,
                'transaction_id': str(transaction_id)
            },
            yo_response=yo_payment_request
        )
        logger.info("Yo momo_collect: tx=%s response=%s", transaction_id, response)
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
        try:
            yo_request = requests.post(
                API_URL,
                data=post_data,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("Yo momo_check_transaction request failed: %s", exc)
            return False
        response = self.interprete_response(
            request_type='momo_check_transaction',
            request_body={'yo_transaction_reference': yo_transaction_reference},
            yo_response=yo_request
        )
        logger.info("Yo momo_check_transaction: ref=%s response=%s",
                     yo_transaction_reference, response)
        return True

    def send_sms(self, message: str, to: str):
        if config('ENV', default='dev') in ['prod', 'test']:
            yo_request = f"http://smgw1.yo.co.ug:9100/sendsms?ybsacctno={self.YO_SMS_ACCOUNT_NO}&password={self.YO_SMS_PASSWORD}&origin=Dinify&sms_content={message}&destinations={to}&nostore=0"  # noqa
            try:
                requests.get(yo_request, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.error("Yo SMS send failed to %s: %s", to, exc)
                return False
        return True

    def process_yo_response(self, response_id):
        try:
            yo_response = MONGO_DB[COL_YO_RESPONSES].find_one({'_id': ObjectId(response_id)})
        except Exception as e:
            logger.error("Failed to read Yo response %s from MongoDB: %s", response_id, e)
            return
        logger.info("Processing Yo Response: %s", response_id)

        if yo_response.get('response_dict') is None:
            logger.debug("Skipping Yo response %s: no response_dict", response_id)
            flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
            return

        request_type = yo_response.get('request_type')
        logger.info("Yo request type: %s", request_type)

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
            logger.info("Processing transaction status check...")
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
                    logger.warning("No Yo transaction found for ref=%s", aggregator_reference)

                if txs_record is None:
                    flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
                    return

                aggregator_status = response_dict.get('TransactionStatus')
                logger.info("Aggregator Status: %s", aggregator_status)
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
                    logger.info("Transaction %s updated to CONFIRMED", aggregator_reference)
                else:
                    return

                flag_doc_as_processed(collection_name=COL_YO_RESPONSES, doc_id=response_id)
        else:
            return
