import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings


def _fernet():
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value):
    if not value:
        return ""
    return _fernet().decrypt(value.encode()).decode()
