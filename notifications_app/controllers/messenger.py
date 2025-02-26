from dinify_backend import settings
from django.core.mail import EmailMessage
# from payment_integrations_app.controllers.yo_integrations import YoIntegration
import requests
from decouple import config


class Messenger():
    def __init__(self):
        self.from_email = ''+settings.EMAIL_HOST_USER
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
            # to=[''],
            # cc=[],
            from_email=self.from_email
        )
        message.content_subtype = 'html'
        message.send(fail_silently=False)

    # def send_sms0(self, msisdn, message):
    #     YoIntegration().send_sms(
    #         to=msisdn,
    #         message=message
    #     )

    def send_sms(self, message: str, msisdn: str):
        # TODO investigate cause of circular import 
        if config('ENV') in ['prod', 'test']:
            yo_request = f"http://smgw1.yo.co.ug:9100/sendsms?ybsacctno={self.YO_SMS_ACCOUNT_NO}&password={self.YO_SMS_PASSWORD}&origin=Dinify&sms_content={message}&destinations={msisdn}&nostore=0"  # noqa
            requests.get(yo_request)
        return True
