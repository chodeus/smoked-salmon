import pytest

from salmon.common.redaction import redact_tracker_fields, redact_tracker_headers, scrub_secrets


@pytest.mark.parametrize(("shortest", "kept", "masked"), [(4, "abc", "abcd"), (8, "abcdefg", "abcdefgh")])
def test_scrub_secrets_masks_a_known_value_only_from_its_minimum_length(shortest, kept, masked) -> None:
    shown = scrub_secrets(f"{kept} | {masked}", [kept, masked], shortest)

    assert shown == f"{kept} | [REDACTED]"


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("{'authkey': 'FAKEKEYONE', 'id': 5}", "{'authkey': '[REDACTED]', 'id': 5}"),
        ("{'passkey': \"FAKE'KEYTWO\"}", "{'passkey': \"[REDACTED]\"}"),
        ('var authkey = "FAKEKEYTHREE";', 'var authkey = "[REDACTED]";'),
        ("{auth: 'FAKEKEYFOUR'}", "{auth: '[REDACTED]'}"),
        ('{"authkey": "FAKE\\"KEYFIVE"}', '{"authkey": "[REDACTED]"}'),
        ("{'authkey': 'FAKEKEYSIX", "{'authkey': '[REDACTED]"),
    ],
)
def test_redact_tracker_fields_masks_a_quoted_value_in_json_a_repr_or_a_script(text, shown) -> None:
    assert redact_tracker_fields(text) == shown


@pytest.mark.parametrize("passkey", ["FAKEPASSKEY0123456789", "FAKE01"])
def test_redact_tracker_fields_masks_an_announce_passkey_in_free_text(passkey) -> None:
    text = f"could not reach https://tracker.example/{passkey}/announce: timed out"

    assert redact_tracker_fields(text) == "could not reach https://tracker.example/[REDACTED]/announce: timed out"


@pytest.mark.parametrize(
    "text", ["author = 'A Name'", '"authors": "A Name, B Name"', "https://tracker.example/torrents/download"]
)
def test_redact_tracker_fields_leaves_a_longer_name_or_another_path_alone(text) -> None:
    assert redact_tracker_fields(text) == text


def test_redact_tracker_fields_ends_a_quoted_value_with_its_line() -> None:
    shown = redact_tracker_fields("session: 'open\nthe next line's words stay")

    assert shown == "session: '[REDACTED]\nthe next line's words stay"


def test_redact_tracker_fields_masks_an_input_a_cut_off_value_runs_into() -> None:
    shown = redact_tracker_fields("{'authkey': 'FAKEKEYEIGHT <input value='FAKEKEYNINE' name='auth'>")

    assert "FAKEKEY" not in shown


def test_response_headers_mask_cookie_values_credentials_and_known_secrets() -> None:
    headers = [
        ("Set-Cookie", "session=new-session-0123; path=/; HttpOnly"),
        ("Set-Cookie", "keeplogged=keep-0123; path=/"),
        ("Set-Cookie", "cf_clearance=clear-0123; path=/"),
        ("X-Api-Token", "token-0123"),
        ("Location", "/torrents.php?id=1&authkey=key-0123"),
        ("Server", "nginx known-secret-0123"),
    ]

    shown = redact_tracker_headers(headers, lambda text: scrub_secrets(text, {"known-secret-0123"}))

    # A newly issued cookie is no known secret, yet its value is masked; the name and attributes stay.
    assert shown["Set-Cookie"].startswith("session=[REDACTED]")
    assert ", keeplogged=[REDACTED]" in shown["Set-Cookie"]
    assert "path=/" in shown["Set-Cookie"]
    assert "new-session-0123" not in shown["Set-Cookie"] and "keep-0123" not in shown["Set-Cookie"]
    # A cookie the scrub knows nothing about: only the header rule masks it.
    assert ", cf_clearance=[REDACTED]; path=/" in shown["Set-Cookie"]
    assert shown["X-Api-Token"] == "[REDACTED]"
    assert shown["Location"] == "/torrents.php?id=1&authkey=[REDACTED]"
    assert shown["Server"] == "nginx [REDACTED]"
