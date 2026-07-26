from app.services.crypto import decrypt_api_key, encrypt_api_key


def test_api_key_encrypts_and_decrypts_without_plaintext_storage():
    api_key = "sk-test-agent-key"
    encrypted = encrypt_api_key(api_key)

    assert encrypted != api_key
    assert decrypt_api_key(encrypted) == api_key


def test_encryption_produces_distinct_ciphertext():
    assert encrypt_api_key("same-key") != encrypt_api_key("same-key")
