import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_kaggle_auth as auth


class KaggleAuthenticationTests(unittest.TestCase):
    def test_auto_uses_platform_without_reading_secrets_and_restores_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            platform_file = Path(temporary) / "platform-token"
            platform_file.write_text("platform-placeholder")
            environment = {"KAGGLE_KERNEL_RUN_TYPE": "Batch", "KAGGLE_DATA_PROXY_URL": "https://proxy.invalid",
                           "KAGGLE_API_V1_TOKEN": str(platform_file), "KAGGLE_API_TOKEN": "personal-placeholder"}
            verifier, secret = Mock(), Mock()
            with patch.dict(os.environ, environment, clear=True), patch("sys.stdout", new_callable=io.StringIO) as output:
                before = dict(os.environ)
                with auth.kaggle_auth("auto", verifier=verifier, secret_loader=secret) as info:
                    self.assertEqual(info["source"], "platform")
                    self.assertNotIn("KAGGLE_API_TOKEN", os.environ)
                    self.assertEqual(os.environ["KAGGLE_API_V1_TOKEN"], str(platform_file))
                self.assertEqual(dict(os.environ), before)
                verifier.assert_called_once_with("platform-placeholder", auth.OWNER)
                secret.assert_not_called()
                self.assertNotIn("platform-placeholder", output.getvalue())
                self.assertNotIn("personal-placeholder", output.getvalue())

    def test_personal_overrides_sdk_platform_priority_and_restores_on_failure(self):
        environment = {"KAGGLE_API_V1_TOKEN": "/not-read", "KAGGLE_API_TOKEN": "personal-placeholder"}
        verifier, secret = Mock(), Mock()
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "producer error"):
                with auth.kaggle_auth("personal", verifier=verifier, secret_loader=secret) as info:
                    self.assertEqual(info["source"], "environment")
                    self.assertNotIn("KAGGLE_API_V1_TOKEN", os.environ)
                    self.assertEqual(os.environ["KAGGLE_API_TOKEN"], "personal-placeholder")
                    raise RuntimeError("producer error")
            self.assertEqual(dict(os.environ), environment)
        verifier.assert_called_once_with("personal-placeholder", auth.OWNER)
        secret.assert_not_called()

    def test_auto_uses_environment_when_platform_is_unavailable(self):
        with patch.dict(os.environ, {"KAGGLE_API_TOKEN": "personal-placeholder"}, clear=True):
            secret = Mock()
            with auth.kaggle_auth("auto", verifier=Mock(), secret_loader=secret) as info:
                self.assertEqual(info["source"], "environment")
            secret.assert_not_called()

    def test_platform_mode_does_not_fall_back_to_personal_or_secret(self):
        with patch.dict(os.environ, {"KAGGLE_API_TOKEN": "personal-placeholder"}, clear=True):
            secret = Mock()
            with self.assertRaisesRegex(auth.KaggleAuthenticationError, "platform credential is unavailable"):
                with auth.kaggle_auth("platform", verifier=Mock(), secret_loader=secret):
                    self.fail("unreachable")
            secret.assert_not_called()

    def test_invalid_selected_credential_does_not_try_secret(self):
        verifier = Mock(side_effect=auth.KaggleAuthenticationError("selected token rejected"))
        secret = Mock()
        with patch.dict(os.environ, {"KAGGLE_API_TOKEN": "personal-placeholder"}, clear=True):
            before = dict(os.environ)
            with self.assertRaisesRegex(auth.KaggleAuthenticationError, "selected token rejected"):
                with auth.kaggle_auth("auto", verifier=verifier, secret_loader=secret):
                    self.fail("unreachable")
            self.assertEqual(dict(os.environ), before)
            secret.assert_not_called()

    def test_missing_sources_load_secret_only_once(self):
        secret, verifier = Mock(return_value=" secret-placeholder "), Mock()
        with patch.dict(os.environ, {}, clear=True), patch.object(auth, "_personal_token", return_value=(None, None)):
            with auth.kaggle_auth("auto", verifier=verifier, secret_loader=secret) as info:
                self.assertEqual(info["source"], "secret")
                self.assertEqual(os.environ["KAGGLE_API_TOKEN"], "secret-placeholder")
            self.assertEqual(dict(os.environ), {})
        secret.assert_called_once_with()
        verifier.assert_called_once_with("secret-placeholder", auth.OWNER)

    def test_environment_token_can_be_read_from_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "token"
            token_file.write_text("from-file-placeholder\n")
            verifier = Mock()
            with patch.dict(os.environ, {"KAGGLE_API_TOKEN": str(token_file)}, clear=True):
                with auth.kaggle_auth("personal", verifier=verifier) as info:
                    self.assertEqual(info["source"], "environment_file")
                    self.assertEqual(os.environ["KAGGLE_API_TOKEN"], "from-file-placeholder")
            verifier.assert_called_once_with("from-file-placeholder", auth.OWNER)

    def test_empty_environment_token_file_does_not_fall_through(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "token"
            token_file.write_text("")
            secret = Mock()
            with patch.dict(os.environ, {"KAGGLE_API_TOKEN": str(token_file)}, clear=True):
                with self.assertRaisesRegex(auth.KaggleAuthenticationError, "empty"):
                    with auth.kaggle_auth("personal", verifier=Mock(), secret_loader=secret):
                        self.fail("unreachable")
            secret.assert_not_called()

    def test_secret_error_does_not_expose_service_response_or_credential(self):
        service = SimpleNamespace(UserSecretsClient=lambda: SimpleNamespace(
            get_secret=Mock(side_effect=RuntimeError("do-not-print-this-credential"))))
        with patch.dict(sys.modules, {"kaggle_secrets": service}):
            with self.assertRaises(auth.KaggleAuthenticationError) as caught:
                auth._secret_token()
        self.assertNotIn("do-not-print-this-credential", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_unknown_mode_does_not_read_sources(self):
        with patch.object(auth, "_personal_token") as personal:
            with self.assertRaises(auth.KaggleAuthenticationError):
                with auth.kaggle_auth("guess"):
                    self.fail("unreachable")
            personal.assert_not_called()

    def test_introspection_is_bounded_and_checks_expected_owner(self):
        response = Mock()
        response.read.return_value = json.dumps({"active": True, "username": auth.OWNER}).encode()
        opener = Mock()
        opener.return_value.__enter__ = Mock(return_value=response)
        opener.return_value.__exit__ = Mock(return_value=False)
        with patch.object(auth, "urlopen", opener):
            self.assertEqual(auth.verify_token("token-placeholder"), auth.OWNER)
            self.assertEqual(opener.call_args.kwargs["timeout"], 40)
            self.assertEqual(opener.call_args.args[0].get_header("User-agent"), "kaggle-api/v1.7.0")
            response.read.return_value = b'{"active":true,"username":"another-owner"}'
            with self.assertRaisesRegex(auth.KaggleAuthenticationError, "different account"):
                auth.verify_token("token-placeholder")
            response.read.return_value = b'{"active":false}'
            with self.assertRaisesRegex(auth.KaggleAuthenticationError, "not active"):
                auth.verify_token("token-placeholder")

    def test_introspection_http_error_has_safe_diagnostic(self):
        error = HTTPError(auth.INTROSPECT_URL, 403, "token-placeholder", {}, None)
        with patch.object(auth, "urlopen", side_effect=error):
            with self.assertRaisesRegex(auth.KaggleAuthenticationError, "HTTP 403") as caught:
                auth.verify_token("token-placeholder")
        self.assertNotIn("token-placeholder", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
