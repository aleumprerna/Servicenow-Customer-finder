import base64
import hashlib

from cryptography.fernet import Fernet

from .config import MicrosoftAuthSettings


def _fernet(settings: MicrosoftAuthSettings) -> Fernet:
    material = settings.token_key or settings.client_secret
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest()))

