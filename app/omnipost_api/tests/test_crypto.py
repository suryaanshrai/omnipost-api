import pytest

from omnipost_api import crypto


def test_round_trip():
    blob = crypto.encrypt("super-secret-token")
    assert crypto.decrypt(blob) == "super-secret-token"


def test_ciphertext_does_not_contain_plaintext():
    blob = crypto.encrypt("super-secret-token")
    serialized = str(blob)
    assert "super-secret-token" not in serialized


def test_aad_binds_ciphertext_to_owner():
    blob = crypto.encrypt("secret", aad=b"channel:1")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob, aad=b"channel:2")
    assert crypto.decrypt(blob, aad=b"channel:1") == "secret"


def test_tampered_ciphertext_fails_closed():
    blob = crypto.encrypt("secret")
    tampered = dict(blob)
    tampered["ciphertext"] = blob["ciphertext"][:-4] + "AAAA"
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(tampered)


def test_encrypt_dict_round_trip():
    values = {"ACCESS_TOKEN": "abc", "REFRESH_TOKEN": "def"}
    blobs = crypto.encrypt_dict(values, aad=b"channel:1")
    assert crypto.decrypt_dict(blobs, aad=b"channel:1") == values


def test_two_encryptions_of_same_plaintext_differ():
    # Random nonce/DEK per call — no ciphertext reuse even for identical input.
    a = crypto.encrypt("same-value")
    b = crypto.encrypt("same-value")
    assert a["ciphertext"] != b["ciphertext"]
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same-value"
