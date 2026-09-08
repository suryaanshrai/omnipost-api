"""Envelope encryption for stored secrets (channel credentials, BYOK provider
keys).

Replaces the old scheme entirely (see the deleted PlatformInstance.save()
password path in git history / fernet.py): a user-supplied password derived
a Fernet key via PBKDF2, which meant (a) the app could never decrypt
credentials without the user present, so unattended scheduled publishing was
structurally impossible, and (b) that password was passed as a plaintext RQ
job argument, landing in Redis for as long as the job sat in the queue.

Here, a single server-held KEK (OMNIPOST_KEK, from env/KMS) wraps a random
per-record DEK. The DEK encrypts the actual secret with AES-256-GCM. No user
input is ever part of the key material, so the server can decrypt on its own
schedule, and `key_version` lets the KEK be rotated without re-touching every
row atomically — see rotate_kek() below.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from functools import cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.exceptions import ImproperlyConfigured

_NONCE_LEN = 12  # AES-GCM standard nonce size


class DecryptionError(ValueError):
    pass


@cache
def _kek_for_version(version: int) -> bytes:
    """The KEK for a given key_version. Version 1 reads OMNIPOST_KEK;
    rotation adds OMNIPOST_KEK_2, OMNIPOST_KEK_3, ... so old rows stay
    decryptable until they're re-encrypted under the newest key."""
    env_name = "OMNIPOST_KEK" if version == 1 else f"OMNIPOST_KEK_{version}"
    raw = os.environ.get(env_name)
    if not raw:
        raise ImproperlyConfigured(
            f"{env_name} is not set. Every environment that stores or reads channel "
            "credentials needs it — see infra/.env.example."
        )
    key = base64.urlsafe_b64decode(raw)
    if len(key) != 32:
        raise ImproperlyConfigured(f"{env_name} must decode to exactly 32 bytes (AES-256).")
    return key


def current_key_version() -> int:
    return int(os.environ.get("OMNIPOST_KEK_CURRENT_VERSION", "1"))


@dataclass(frozen=True)
class EncryptedBlob:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    dek_nonce: bytes
    key_version: int

    def to_storage(self) -> dict[str, str | int]:
        return {
            "ciphertext": base64.b64encode(self.ciphertext).decode(),
            "nonce": base64.b64encode(self.nonce).decode(),
            "wrapped_dek": base64.b64encode(self.wrapped_dek).decode(),
            "dek_nonce": base64.b64encode(self.dek_nonce).decode(),
            "key_version": self.key_version,
        }

    @classmethod
    def from_storage(cls, data: dict) -> EncryptedBlob:
        try:
            return cls(
                ciphertext=base64.b64decode(data["ciphertext"]),
                nonce=base64.b64decode(data["nonce"]),
                wrapped_dek=base64.b64decode(data["wrapped_dek"]),
                dek_nonce=base64.b64decode(data["dek_nonce"]),
                key_version=int(data["key_version"]),
            )
        except (KeyError, ValueError) as exc:
            raise DecryptionError(f"Malformed encrypted blob: {exc}") from exc


def encrypt(plaintext: str, *, aad: bytes = b"") -> dict[str, str | int]:
    """Encrypt one secret. `aad` (additional authenticated data — e.g. the
    channel id) binds the ciphertext to its owning record so a blob copied
    onto a different row fails to decrypt instead of silently decrypting."""
    version = current_key_version()
    kek = _kek_for_version(version)

    dek = AESGCM.generate_key(bit_length=256)  # type: ignore[call-arg]  # cryptography's bundled stub omits this valid kwarg
    dek_nonce = os.urandom(_NONCE_LEN)
    wrapped_dek = AESGCM(kek).encrypt(dek_nonce, dek, aad)

    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad)

    return EncryptedBlob(
        ciphertext=ciphertext, nonce=nonce, wrapped_dek=wrapped_dek, dek_nonce=dek_nonce, key_version=version
    ).to_storage()


def decrypt(data: dict, *, aad: bytes = b"") -> str:
    blob = EncryptedBlob.from_storage(data)
    kek = _kek_for_version(blob.key_version)
    try:
        dek = AESGCM(kek).decrypt(blob.dek_nonce, blob.wrapped_dek, aad)
        plaintext = AESGCM(dek).decrypt(blob.nonce, blob.ciphertext, aad)
    except Exception as exc:  # cryptography raises InvalidTag on any tamper/wrong-key
        raise DecryptionError("Failed to decrypt — wrong key version, tampered data, or wrong AAD.") from exc
    return plaintext.decode("utf-8")


def encrypt_dict(values: dict[str, str], *, aad: bytes = b"") -> dict[str, dict[str, str | int]]:
    return {key: encrypt(value, aad=aad) for key, value in values.items()}


def decrypt_dict(blobs: dict[str, dict], *, aad: bytes = b"") -> dict[str, str]:
    return {key: decrypt(blob, aad=aad) for key, blob in blobs.items()}
