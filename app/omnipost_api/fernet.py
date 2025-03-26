from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
import os

class FernetEncryption:
    @staticmethod
    def generate_key_from_password(password, salt=None):
        """Convert a user password into a Fernet key"""
        if isinstance(password, str):
            password = password.encode('utf-8')

        # Generate a salt if not provided
        if salt is None:
            salt = os.urandom(16)

        # Create a key derivation function
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )

        # Derive a key from the password
        key = base64.urlsafe_b64encode(kdf.derive(password))

        return key, salt

    @staticmethod
    def encrypt_string(string_to_encrypt, user_key, salt=None):
        """Encrypt a string using a user-provided key"""
        if isinstance(string_to_encrypt, str):
            string_to_encrypt = string_to_encrypt.encode('utf-8')

        # Generate a Fernet key from the user key
        key, salt = FernetEncryption.generate_key_from_password(user_key, salt)

        # Create a Fernet object
        f = Fernet(key)

        # Encrypt the string
        encrypted_string = f.encrypt(string_to_encrypt)

        # Return both the encrypted string and the salt (needed for decryption)
        return encrypted_string, salt

    @staticmethod
    def decrypt_string(encrypted_string, user_key, salt):
        """Decrypt a string using the user-provided key and salt"""
        # Generate the same Fernet key using the user key and stored salt
        key, _ = FernetEncryption.generate_key_from_password(user_key, salt)

        # Create a Fernet object
        f = Fernet(key)

        # Decrypt the string
        decrypted_string = f.decrypt(encrypted_string)

        return decrypted_string.decode('utf-8')

# Example usage
if __name__ == "__main__":
    # The secure string you want to protect
    secure_string = "This is my confidential information"
    
    # User provides this key/password
    user_key = "user_secret_password"
    
    # Encrypt the string
    encrypted_data, salt = FernetEncryption.encrypt_string(secure_string, user_key)
    
    # Store these values in your database or file system
    # - encrypted_data: The encrypted string
    # - salt: The salt used for key derivation
    
    print(f"Encrypted: {encrypted_data}")
    print(f"Salt: {base64.b64encode(salt).decode('utf-8')}")
    
    # Later, when the user wants to access the secure string:
    try:
        # User provides the same key
        recovered_string = FernetEncryption.decrypt_string(encrypted_data, user_key, salt)
        print(f"Decrypted: {recovered_string}")
        
        # Test with wrong key
        wrong_key = "wrong_password"
        recovered_string = FernetEncryption.decrypt_string(encrypted_data, wrong_key, salt)
        print(f"Decrypted with wrong key: {recovered_string}")  # Should fail
    except Exception as e:
        print(f"Decryption failed: {e}")