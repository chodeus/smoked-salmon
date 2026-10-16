import pytest

from salmon.common.redaction import redact_tracker_fields, scrub_secrets


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
