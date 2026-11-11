from smb_sync.credentials import PASSWORD_LENGTH, generate_password


def test_generated_password_shape():
    password = generate_password()
    assert len(password) == PASSWORD_LENGTH
    assert password.isalnum()
