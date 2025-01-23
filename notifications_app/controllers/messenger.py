from dinify_backend import settings
from django.core.mail import EmailMessage
from payment_integrations_app.controllers.yo_integrations import YoIntegration


class Messenger():
    def __init__(self):
        self.from_email = ''+settings.EMAIL_HOST_USER

    def send_email(self, to: list, cc: list, subject: str, message: str) -> bool:
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

    def send_sms(self, msisdn, message):
        YoIntegration().send_sms(
            to=msisdn,
            message=message
        )
