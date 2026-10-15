import pytest

from salmon.common.redaction import scrub_secrets


@pytest.mark.parametrize(("shortest", "kept", "masked"), [(4, "abc", "abcd"), (8, "abcdefg", "abcdefgh")])
def test_scrub_secrets_masks_a_known_value_only_from_its_minimum_length(shortest, kept, masked) -> None:
    shown = scrub_secrets(f"{kept} | {masked}", [kept, masked], shortest)

    assert shown == f"{kept} | [REDACTED]"
