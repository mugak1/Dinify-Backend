import logging
import requests
import json
from decouple import config
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


@dataclass
class Pesapal:
    pesapal_domain: str = 'https://cybqa.pesapal.com/pesapalv3/api'
    HEADERS: dict = None

    def __post_init__(self):
        if self.HEADERS is None:
            self.HEADERS = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            }

    def authenticate(self):
        URL = f"{self.pesapal_domain}/Auth/RequestToken"
        post_body = {
            'consumer_key': config('PESAPAL_CONSUMER_KEY'),
            'consumer_secret': config('PESAPAL_CONSUMER_SECRET')
        }
        try:
            response = requests.post(
                URL,
                data=json.dumps(post_body),
                headers=self.HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response = response.json()
        except requests.RequestException as exc:
            logger.error("Pesapal authentication failed: %s", exc)
            return {'status': 500, 'message': 'Pesapal authentication request failed'}
        response['status'] = 200
        return response
