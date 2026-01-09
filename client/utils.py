import os
import json
from cryptography.fernet import Fernet

def generate_and_save_key(filepath):
    key = Fernet.generate_key()
    with open(filepath, 'wb') as key_file:
        key_file.write(key)
    return key
def retrieve_key(filepath):
    with open(filepath, 'rb') as key_file:
        key = key_file.read()
    return key
def encrypt_json(data, key):
    fernet = Fernet(key)
    json_data = json.dumps(data).encode()
    encrypted_data = fernet.encrypt(json_data)
    return encrypted_data
def decrypt_json(encrypted_data, key):
    fernet = Fernet(key)
    decrypted_data = fernet.decrypt(encrypted_data)
    data = json.loads(decrypted_data.decode())
    return data
