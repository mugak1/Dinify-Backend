import logging
import requests
from dinify_backend import settings
from django.core.mail import EmailMessage
from decouple import config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds


class Messenger():
    def __init__(self):
        self.from_email = '' + settings.EMAIL_HOST_USER
        self.YO_SMS_ACCOUNT_NO = config('YO_SMS_ACCOUNT_NO')
        self.YO_SMS_PASSWORD = config('YO_SMS_PASSWORD')

    def send_email(self, to: list, cc: list, subject: str, message: str) -> bool:
        # if to is not a list, make it a list
        if not isinstance(to, list):
            to = [to]
        message = EmailMessage(
            subject=subject,
            body=message,
            to=to,
            cc=cc,
            from_email=self.from_email
        )
        message.content_subtype = 'html'
        message.send(fail_silently=False)

    def send_sms(self, message: str, msisdn: str):
        if config('ENV', default='dev') in ['prod', 'test']:
            yo_request = f"http://smgw1.yo.co.ug:9100/sendsms?ybsacctno={self.YO_SMS_ACCOUNT_NO}&password={self.YO_SMS_PASSWORD}&origin=Dinify&sms_content={message}&destinations={msisdn}&nostore=0"  # noqa
            try:
                requests.get(yo_request, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.error("SMS send failed to %s: %s", msisdn, exc)
                return False
        return True
