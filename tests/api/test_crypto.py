import pytest

from traceloom.llm.crypto import CryptoError, SecretBox, generate_key, hint


def test_round_trip_and_ciphertext_differs():
    box = SecretBox(generate_key())
    token = box.encrypt("sk-secret-1234")
    assert token != "sk-secret-1234" and box.decrypt(token) == "sk-secret-1234"


def test_rotated_key_still_decrypts_old_data():
    old, new = generate_key(), generate_key()
    token = SecretBox(old).encrypt("sk-abc")
    rotated = SecretBox(f"{new},{old}")
    assert rotated.decrypt(token) == "sk-abc"
    fresh = rotated.encrypt("sk-new")
    assert SecretBox(new).decrypt(fresh) == "sk-new"  # new data uses the first key
    assert SecretBox(new).decrypt(rotated.rotate(token)) == "sk-abc"


def test_removed_key_cannot_decrypt():
    token = SecretBox(generate_key()).encrypt("x")
    with pytest.raises(CryptoError):
        SecretBox(generate_key()).decrypt(token)


@pytest.mark.parametrize("keys", ["", " , ", "not-a-key"])
def test_bad_key_config(keys):
    with pytest.raises(CryptoError):
        SecretBox(keys)


def test_hint_shows_last_four():
    assert hint("sk-abcdef1234") == "…1234"
