import logging
from urllib.parse import parse_qs

from decouple import config
from django.core.management.base import BaseCommand, CommandError

from notifications_app.controllers.sms import YO_SMS_URL, send_sms

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Send ONE test SMS through the consolidated Yo sender and print exactly '
        'what the gateway said. Target: --to <msisdn>, else TEST_SMS_RECIPIENT '
        'from the environment.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            dest='to',
            default=None,
            help='Destination msisdn (canonical 256XXXXXXXXX). '
                 'Defaults to TEST_SMS_RECIPIENT when set.',
        )

    def handle(self, *args, **options):
        msisdn = options['to'] or config('TEST_SMS_RECIPIENT', default=None)
        if not msisdn:
            raise CommandError(
                'No destination: pass --to <msisdn> or set TEST_SMS_RECIPIENT '
                'in the environment. This command never hardcodes a number.'
            )

        self.stdout.write(f'Sending one test SMS to {msisdn} via {YO_SMS_URL} ...')

        # bypass_env_gate=True is DELIBERATE: this command exists to verify
        # gateway credentials BEFORE an ENV change (e.g. while the server still
        # runs ENV=dev), so it cannot be gated by the very flag it exists to
        # test. `capture` hands back the raw exchange for the report below.
        exchange = {}
        result = send_sms(
            message='Dinify test SMS - please ignore.',
            msisdn=msisdn,
            bypass_env_gate=True,
            capture=exchange,
        )

        if 'status_code' in exchange:
            parsed = parse_qs(exchange['body'] or '')
            status_values = parsed.get('ybs_autocreate_status', ['<missing>'])
            states = parsed.get('ybs_autocreate_message', [])

            self.stdout.write(f"HTTP status:            {exchange['status_code']}")
            self.stdout.write(f"ybs_autocreate_status:  {status_values[0]}")
            if states:
                for state in states:
                    self.stdout.write(f'destination state:      {state}')
            else:
                self.stdout.write('destination state:      <none reported>')
            self.stdout.write(f"raw body:               {exchange['body']}")
        else:
            self.stderr.write('No HTTP response captured (transport failure - see log above).')

        if result:
            self.stdout.write(self.style.SUCCESS('Gateway accepted the message.'))
        else:
            self.stderr.write('Gateway did NOT accept the message.')
