"""Tests for the inbound webhook — the app's only externally-reachable ingress.

This is the one adapter where a mistake is directly exploitable: everything else
POLLS a provider the operator configured, while this ACCEPTS input from whoever can
reach the port. It had no test coverage at all.

Ordered by blast radius:

1. **Fail-closed.** Disabled, or no signing secret, rejects everything — enabling
   the app must never open an unauthenticated path that manufactures board work.
2. **Forgery is refused.** Missing, wrong, truncated, and other-body signatures.
3. **Nothing unauthenticated is parsed.** Signature is checked BEFORE `json.loads`,
   and an oversized body is refused before it is hashed.
4. **Input validation.** Non-object payloads, missing titles, and field lengths.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    set_top_level,
    webhook,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.routes import (
    _webhook_reject_status,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import put_secret

_SECRET = "unit-test-signing-secret"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _body(**kw: object) -> bytes:
    payload: dict[str, object] = {"title": "Disk 91% on web-3", "severity": "warning"}
    payload.update(kw)
    return json.dumps(payload).encode("utf-8")


class _Env(unittest.TestCase):
    """Isolated data home, drained queue, provider enabled + secret set.

    The home is isolated HERE rather than by a fixture: these tests live under
    ``src/``, so ``test/conftest.py`` (whose autouse fixture pins ``KIROCREW_HOME``)
    never loads for them — the sibling app tests all do the same. Without it a
    "no secret configured must reject" assertion passes only because another test
    wrote one, which is exactly what a fail-closed test must not do.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self._tmp.name
        webhook.drain()

    def tearDown(self) -> None:
        webhook.drain()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        self._tmp.cleanup()

    @staticmethod
    def _enable(*, secret: str | None = _SECRET) -> None:
        set_top_level("providers", {"webhook": {"enabled": True}})
        if secret is not None:
            put_secret(webhook.PROVIDER_ID, "signing_secret", secret)


class TestFailClosed(_Env):
    def test_disabled_rejects_even_a_valid_signature(self) -> None:
        """A correct signature must not be enough when the source is off."""
        put_secret(webhook.PROVIDER_ID, "signing_secret", _SECRET)
        body = _body()
        accepted, detail = webhook.enqueue(body, _sign(body))
        self.assertFalse(accepted)
        self.assertIn("not enabled", detail)

    def test_no_secret_rejects_everything(self) -> None:
        """Enabling without a secret must not become an open endpoint."""
        self._enable(secret=None)
        body = _body()
        accepted, detail = webhook.enqueue(body, _sign(body))
        self.assertFalse(accepted)
        self.assertIn("no signing secret", detail)

    def test_nothing_is_queued_by_a_rejected_delivery(self) -> None:
        self._enable()
        webhook.enqueue(_body(), "")
        self.assertEqual(webhook.queue_depth(), 0)


class TestSignature(_Env):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_valid_signature_is_accepted(self) -> None:
        body = _body(id="disk-web3")
        accepted, detail = webhook.enqueue(body, _sign(body))
        self.assertTrue(accepted, detail)
        self.assertEqual(webhook.queue_depth(), 1)

    def test_uppercase_signature_is_accepted(self) -> None:
        """Senders differ on hex case; rejecting it would be a false negative."""
        body = _body()
        accepted, _ = webhook.enqueue(body, _sign(body).upper())
        self.assertTrue(accepted)

    def test_missing_signature_is_rejected(self) -> None:
        accepted, detail = webhook.enqueue(_body(), "")
        self.assertFalse(accepted)
        self.assertEqual(detail, "signature mismatch")

    def test_wrong_signature_is_rejected(self) -> None:
        accepted, _ = webhook.enqueue(_body(), "0" * 64)
        self.assertFalse(accepted)

    def test_truncated_valid_signature_is_rejected(self) -> None:
        """A prefix must not pass — the guard against a sloppy compare."""
        body = _body()
        accepted, _ = webhook.enqueue(body, _sign(body)[:32])
        self.assertFalse(accepted)

    def test_signature_for_a_different_body_is_rejected(self) -> None:
        accepted, _ = webhook.enqueue(_body(), _sign(b'{"title":"something else"}'))
        self.assertFalse(accepted)

    def test_tampered_body_with_captured_signature_is_rejected(self) -> None:
        """The realistic attack: replay a real signature over an edited body."""
        original = _body(severity="warning")
        signature = _sign(original)
        tampered = _body(severity="critical")
        accepted, _ = webhook.enqueue(tampered, signature)
        self.assertFalse(accepted)

    def test_a_different_secret_does_not_validate(self) -> None:
        body = _body()
        accepted, _ = webhook.enqueue(body, _sign(body, "some-other-secret"))
        self.assertFalse(accepted)

    def test_verify_signature_is_constant_time(self) -> None:
        """Pin the use of compare_digest rather than ``==``."""
        import inspect

        source = inspect.getsource(webhook.verify_signature)
        self.assertIn("compare_digest", source)


class TestUnauthenticatedInputIsNeverParsed(_Env):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_malformed_json_fails_on_the_signature_first(self) -> None:
        """An unsigned body must be refused for its SIGNATURE, not its syntax.

        If the order ever inverts, the endpoint parses attacker-controlled bytes
        before establishing any trust.
        """
        accepted, detail = webhook.enqueue(b"not json at all", "")
        self.assertFalse(accepted)
        self.assertEqual(detail, "signature mismatch")

    def test_oversized_body_is_refused_before_hashing(self) -> None:
        """Size is checked before the HMAC, so a huge body costs no hash."""
        huge = b"x" * (webhook.MAX_BODY_BYTES + 1)
        accepted, detail = webhook.enqueue(huge, _sign(huge))
        self.assertFalse(accepted)
        self.assertEqual(detail, "body too large")

    def test_validly_signed_malformed_json_is_a_payload_error(self) -> None:
        raw = b"not json at all"
        accepted, detail = webhook.enqueue(raw, _sign(raw))
        self.assertFalse(accepted)
        self.assertEqual(detail, "malformed JSON")


class TestPayloadValidation(_Env):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def _send(self, raw: bytes) -> tuple[bool, str]:
        return webhook.enqueue(raw, _sign(raw))

    def test_non_object_payload_is_rejected(self) -> None:
        for raw in (b"[1,2]", b'"a string"', b"42", b"null"):
            accepted, detail = self._send(raw)
            self.assertFalse(accepted, raw)
            self.assertEqual(detail, "payload must be a JSON object", raw)

    def test_missing_title_is_rejected(self) -> None:
        """A signal with no title is an unreadable board row."""
        accepted, detail = self._send(b'{"severity":"warning"}')
        self.assertFalse(accepted)
        self.assertEqual(detail, "payload has no title")

    def test_blank_title_is_rejected(self) -> None:
        accepted, detail = self._send(b'{"title":"   "}')
        self.assertFalse(accepted)
        self.assertEqual(detail, "payload has no title")

    def test_summary_is_accepted_as_a_title(self) -> None:
        """Alertmanager-style payloads use `summary`."""
        accepted, _ = self._send(b'{"summary":"Broker unreachable"}')
        self.assertTrue(accepted)

    def test_long_fields_are_capped(self) -> None:
        raw = json.dumps(
            {"title": "t", "resource": "r" * 500, "url": "u" * 900, "id": "i" * 500}
        ).encode()
        accepted, _ = self._send(raw)
        self.assertTrue(accepted)
        signal = webhook.drain()[0]
        self.assertLessEqual(len(signal.resource), 200)
        self.assertLessEqual(len(signal.url), 500)

    def test_non_dict_labels_do_not_raise(self) -> None:
        accepted, _ = self._send(b'{"title":"t","labels":"not-a-dict"}')
        self.assertTrue(accepted)

    def test_drain_empties_the_queue(self) -> None:
        self._send(_body(id="a"))
        self._send(_body(id="b"))
        self.assertEqual(webhook.queue_depth(), 2)
        self.assertEqual(len(webhook.drain()), 2)
        self.assertEqual(webhook.queue_depth(), 0)


class TestRejectStatusMapping(unittest.TestCase):
    """A payload fault is not an auth failure.

    Everything used to return 401, so a sender debugging a bad body was told
    "Unauthorized" and would re-check credentials that were fine — while a genuine
    signature failure looked identical to a typo.
    """

    def test_trust_failures_are_401(self) -> None:
        for detail in (
            "webhook source is not enabled",
            "no signing secret configured",
            "signature mismatch",
        ):
            self.assertEqual(_webhook_reject_status(detail), 401, detail)

    def test_payload_faults_are_400(self) -> None:
        for detail in (
            "malformed JSON",
            "payload must be a JSON object",
            "payload has no title",
        ):
            self.assertEqual(_webhook_reject_status(detail), 400, detail)

    def test_oversized_body_is_413(self) -> None:
        self.assertEqual(_webhook_reject_status("body too large"), 413)

    def test_unknown_reason_defaults_to_401(self) -> None:
        """A newly-added rejection must not be advertised as 'request was fine'."""
        self.assertEqual(_webhook_reject_status("something new"), 401)

    def test_every_enqueue_rejection_reason_is_mapped(self) -> None:
        """Derived from the source so a new reason cannot silently default.

        Catches the case where someone adds a rejection to ``enqueue`` and forgets
        that its status has to be classified.
        """
        import inspect
        import re

        source = inspect.getsource(webhook.enqueue)
        reasons = set(re.findall(r'return False, "([^"]+)"', source))
        self.assertTrue(reasons, "no literal rejection reasons found")
        known = _WEBHOOK_KNOWN_REASONS
        unmapped = reasons - known
        self.assertFalse(
            unmapped,
            f"unclassified webhook rejection reason(s): {sorted(unmapped)} — add them to "
            "_WEBHOOK_AUTH_REJECTIONS or the payload/size branches in "
            "_webhook_reject_status, and to this test's known set",
        )


#: Every rejection reason ``enqueue`` can return, with its status deliberately
#: chosen. Kept next to the test that enforces completeness.
_WEBHOOK_KNOWN_REASONS = frozenset(
    {
        "webhook source is not enabled",
        "no signing secret configured",
        "body too large",
        "signature mismatch",
        "malformed JSON",
        "payload must be a JSON object",
        "payload has no title",
    }
)


if __name__ == "__main__":
    unittest.main()
