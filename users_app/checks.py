"""
System checks for users_app.

The D11 abuse limits are read from settings on every use and FAIL CLOSED there, so a
bad value on the host would turn every OTP route into a 500. This check reports the
same problem earlier: the deploy runs ``manage.py check --deploy`` before it restarts
Apache, and an error here stops it with the old workers still serving.
"""
from django.core.checks import Error, register

from users_app import otp_limits


@register()
def check_abuse_limits(app_configs, **kwargs):
    return [
        Error(message, id='users_app.E001',
              hint='See docs/engineering/d11-enforcement.md for the accepted ranges.')
        for message in otp_limits.configuration_errors()
    ]
