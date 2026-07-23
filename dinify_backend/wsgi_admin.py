"""
WSGI entry point for the admin control plane.

Mirrors ``wsgi.py`` but points at ``dinify_backend.settings_admin``. Apache's admin
vhost references this via a DEDICATED ``WSGIDaemonProcess`` (see the founder runbook),
so the customer app's ``DJANGO_SETTINGS_MODULE`` can never pin this process.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dinify_backend.settings_admin')

application = get_wsgi_application()
