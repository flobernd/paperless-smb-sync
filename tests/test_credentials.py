from smb_sync.credentials import PASSWORD_LENGTH, credentials_block, generate_password


def test_generated_password_shape():
    password = generate_password()
    assert len(password) == PASSWORD_LENGTH
    assert password.isalnum()


def test_block_carries_everything_a_scanner_profile_needs():
    block = credentials_block("nas.lan", "inbox", "carol", "s3cret")
    assert "SMB credentials for 'carol'" in block
    assert "\\\\nas.lan\\inbox" in block
    assert "s3cret" in block
    assert "shown only once" in block


def test_block_hints_at_regen_for_the_log():
    block = credentials_block("nas.lan", "inbox", "carol", "s3cret")
    assert "smb-sync regen carol" in block


def test_block_omits_the_regen_hint_when_it_is_not_for_an_admin():
    block = credentials_block("nas.lan", "inbox", "carol", "s3cret", regen_hint=False)
    assert "smb-sync regen" not in block
    # everything the recipient actually needs survives
    assert "\\\\nas.lan\\inbox" in block
    assert "s3cret" in block
    assert "shown only once" in block
