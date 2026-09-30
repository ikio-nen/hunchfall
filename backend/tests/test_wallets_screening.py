"""Unit tests for watch-only screening — no network, no DB."""

import pytest

from app.wallets import InvalidAddress, normalize_address, screen_for_secrets


def test_screen_rejects_key_material():
    assert screen_for_secrets("0x" + "a" * 64)  # 0x-prefixed private key
    assert screen_for_secrets("a" * 64)  # bare 64-hex key
    assert screen_for_secrets("key " + "b" * 64 + " end")  # embedded token
    assert screen_for_secrets("xprv9s21ZrQH143K3abcdef")
    assert screen_for_secrets("my seed phrase lives here")
    assert screen_for_secrets("-----BEGIN PRIVATE KEY-----")
    assert screen_for_secrets("label\x07with-control-char")
    assert screen_for_secrets("alpha " * 11 + "omega")  # 12-word mnemonic shape


def test_screen_allows_normal_values():
    assert not screen_for_secrets("0x" + "ab" * 20)  # a normal address
    assert not screen_for_secrets("Ikio main")
    assert not screen_for_secrets("watch-only")
    assert not screen_for_secrets("")
    assert not screen_for_secrets(None)  # defensive


def test_normalize_address_lowercases_and_rejects():
    assert normalize_address("0x" + "AB" * 20) == "0x" + "ab" * 20
    with pytest.raises(InvalidAddress):
        normalize_address("0x123")
    with pytest.raises(InvalidAddress):
        normalize_address("0x" + "ab" * 32)  # a key, not an address
    with pytest.raises(InvalidAddress):
        normalize_address("not-an-address")
