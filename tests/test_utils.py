"""Tests for the utility functions the server actually calls."""

import string

from qlik_sense_mcp_server.utils import (
    QLIK_SESSION_COOKIE_PREFIX,
    bare_field_name,
    escape_qlik_field_name,
    generate_xrfkey,
    looks_like_qlik_session_name,
    pick_qlik_session_cookie,
)


class TestFieldNameWriting:
    """A field name reaches Qlik in brackets, always.

    A bare name is parsed as an expression, so a two-word field comes back
    as "Garbage after expression" instead of as the field it is.
    """

    def test_plain_name_is_bracketed(self):
        assert escape_qlik_field_name("Amount") == "[Amount]"

    def test_two_word_name_is_bracketed(self):
        assert escape_qlik_field_name("Order Date") == "[Order Date]"

    def test_already_bracketed_name_is_left_alone(self):
        assert escape_qlik_field_name("[Order Date]") == "[Order Date]"

    def test_surrounding_space_is_dropped(self):
        assert escape_qlik_field_name("  Amount  ") == "[Amount]"

    def test_empty_name_stays_empty(self):
        assert escape_qlik_field_name("") == ""

    def test_bare_name_strips_the_brackets(self):
        assert bare_field_name("[Order Date]") == "Order Date"

    def test_bare_name_of_an_unbracketed_name_is_itself(self):
        assert bare_field_name(" Amount ") == "Amount"

    def test_bracketing_and_unbracketing_round_trip(self):
        for name in ("Amount", "Order Date", "Тип ставки"):
            assert bare_field_name(escape_qlik_field_name(name)) == name


class TestXrfkey:
    def test_length_and_alphabet(self):
        key = generate_xrfkey()
        allowed = set(string.ascii_lowercase + string.digits)
        assert len(key) == 16
        assert set(key) <= allowed

    def test_two_keys_differ(self):
        assert generate_xrfkey() != generate_xrfkey()


class TestSessionCookiePicking:
    """Qlik's session cookie can be renamed per virtual proxy, and a load
    balancer adds cookies of its own, so it is matched, not assumed."""

    @staticmethod
    def _jar(**cookies):
        return list(cookies), cookies.get

    def test_conventional_prefix_wins(self):
        names, get = self._jar(**{"X-Qlik-Session-jwt": "abc", "lb-affinity": "xyz"})
        assert pick_qlik_session_cookie(names, get) == ("X-Qlik-Session-jwt", "abc")

    def test_renamed_cookie_is_found_by_the_word_qlik(self):
        names, get = self._jar(**{"lb-affinity": "xyz", "my-qlik-cookie": "abc"})
        assert pick_qlik_session_cookie(names, get) == ("my-qlik-cookie", "abc")

    def test_a_lone_cookie_can_only_be_the_session(self):
        names, get = self._jar(**{"whatever": "abc"})
        assert pick_qlik_session_cookie(names, get) == ("whatever", "abc")

    def test_nothing_matches_in_a_crowded_jar(self):
        names, get = self._jar(**{"lb-affinity": "xyz", "csrf": "abc"})
        assert pick_qlik_session_cookie(names, get) == (None, None)

    def test_name_test_covers_prefix_and_word(self):
        assert looks_like_qlik_session_name(QLIK_SESSION_COOKIE_PREFIX + "-jwt")
        assert looks_like_qlik_session_name("my-QLIK-cookie")
        assert not looks_like_qlik_session_name("lb-affinity")
