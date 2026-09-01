"""A rate-limited pairing code must be reported as a rate limit.

The reported bug: after a session drops, the FIRST pairing code always fails
with "Nenhum código de pareamento foi recebido" and the second one works. From
a real wppconnect.log, a stranded session asked WhatsApp for a fresh code once
a minute for eighteen minutes and then got:

    pairing code failure details: {"name":"CompanionHelloError",
      "type":"{\\"name\\":\\"IQErrorRateOverlimit\\",
      \\"value\\":{\\"text\\":\\"rate-overlimit\\",\\"code\\":429}}"}

The quota is enforced per phone number on WhatsApp's side, so it outlives the
session and the process: by the time the user asks for a code, the background
loop has already spent the allowance. What reached the user was the class name
"CompanionHelloError", which tells them nothing and invites an immediate retry
— the one thing that keeps the quota spent.

checkQrCode v8 now sets ``rateLimited`` on the failure it reports. This is read
three ways because the flag only exists once BOTH the rebuilt WPPConnect Server
and the re-applied node_modules patch are in place, and an install that is only
halfway there still has to get the right message.
"""

import pytest

from core.websocket_client import phone_code_error_is_rate_limit


class TestTheFlagFromCheckQrCodeV8:
    def test_rate_limited_true_is_enough(self):
        assert phone_code_error_is_rate_limit(
            {"name": "CompanionHelloError", "rateLimited": True}
        )

    def test_rate_limited_false_on_an_ordinary_failure(self):
        assert not phone_code_error_is_rate_limit(
            {"name": "TimeoutError", "message": "took too long", "rateLimited": False}
        )


class TestTheRawDetailsFromAnUnpatchedServer:
    """The details blob carries WhatsApp's own answer whatever the
    server-side version, so it is matched as text."""

    REAL_PAYLOAD = {
        "name": "CompanionHelloError",
        "message": "CompanionHelloError",
        "details": {
            "name": "CompanionHelloError",
            "type": '{"name":"IQErrorRateOverlimit",'
                    '"value":{"text":"rate-overlimit","code":429}}',
        },
    }

    def test_the_captured_payload_is_recognised_without_the_flag(self):
        assert phone_code_error_is_rate_limit(self.REAL_PAYLOAD)

    def test_the_bare_class_name_alone_is_not_enough(self):
        # CompanionHelloError is thrown for more than rate limiting; guessing
        # from the class name would show the wrong advice for every other cause.
        assert not phone_code_error_is_rate_limit(
            {"name": "CompanionHelloError", "message": "CompanionHelloError"}
        )

    def test_the_camel_case_spelling_also_counts(self):
        assert phone_code_error_is_rate_limit({"message": "IQErrorRateOverlimit"})


class TestItNeverRaises:
    @pytest.mark.parametrize("bad", [None, "", [], 0, "rate-overlimit"])
    def test_a_non_dict_payload_is_false(self, bad):
        assert phone_code_error_is_rate_limit(bad) is False

    def test_an_empty_payload_is_false(self):
        assert phone_code_error_is_rate_limit({}) is False
