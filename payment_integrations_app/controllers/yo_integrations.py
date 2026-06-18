import logging
import requests
from decouple import config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10  # seconds


class YoIntegration:
    def __init__(self):
        self.YO_SMS_ACCOUNT_NO = config('YO_SMS_ACCOUNT_NO')
        self.YO_SMS_PASSWORD = config('YO_SMS_PASSWORD')

    def send_sms(self, message: str, to: str):
        if config('ENV', default='dev') in ['prod', 'test']:
            yo_request = f"http://smgw1.yo.co.ug:9100/sendsms?ybsacctno={self.YO_SMS_ACCOUNT_NO}&password={self.YO_SMS_PASSWORD}&origin=Dinify&sms_content={message}&destinations={to}&nostore=0"  # noqa
            try:
                requests.get(yo_request, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.error("Yo SMS send failed to %s: %s", to, exc)
                return False
        return True
