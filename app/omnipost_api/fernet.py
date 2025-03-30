import base64
import os
from typing import Dict, Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

class FernetEncryptor:
    def __init__(self, password: str = None, salt: bytes = None):
        """
        Initialize the encryptor with password and optional salt.
        If salt is not provided, a random one will be generated.
        """
        if password is None:
            password = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8')
        self.password = password.encode()
        self.salt = salt or os.urandom(16)  # Generate salt if not provided
        self.fernet = self._create_fernet()
    
    def _create_fernet(self) -> Fernet:
        """Create a Fernet instance using the password and salt."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(self.password))
        return Fernet(key)
    
    def encrypt(self, data: str) -> bytes:
        """Encrypt a string and return bytes."""
        return self.fernet.encrypt(data.encode()).decode()
    
    def decrypt(self, token: str) -> str:
        """Decrypt bytes and return a string."""
        return self.fernet.decrypt(token.encode()).decode()
    
    def encrypt_dict(self, data: Dict[str, str]) -> Dict[str, str]:
        """Encrypt all keys in a dictionary."""
        encrypted_dict = {}
        for key, value in data.items():
            # Encrypt the key
            enc_val = self.encrypt(value)
            encrypted_dict[key] = enc_val
                
        return encrypted_dict
    
    def decrypt_dict_keys(self, data: Dict[str, str]) -> Dict[str, str]:
        """Decrypt all keys in a dictionary."""
        decrypted_dict = {}
        for key, value in data.items():
            # Decrypt the key
            decrypted_value = self.decrypt(value)
            decrypted_dict[key] = decrypted_value
                
        return decrypted_dict
    
    def get_salt_b64(self) -> str:
        """Get the salt as a base64 encoded string for storage."""
        return base64.b64encode(self.salt).decode()
    
    @classmethod
    def from_salt_b64(cls, password: str, salt_b64: str):
        """Create an encryptor instance from a base64 encoded salt string."""
        salt = base64.b64decode(salt_b64)
        return cls(password, salt)