"""Select and verify one Kaggle credential source without logging credential values."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


OWNER = "qiuqiuslowchillchill"
AUTH_MODES = {"auto", "platform", "personal"}
INTROSPECT_URL = "https://api.kaggle.com/v1/security.OAuthService/IntrospectToken"
AUTH_ENVIRONMENT = ("KAGGLE_API_V1_TOKEN", "KAGGLE_API_TOKEN",
                    "POLYMARKET_KAGGLE_AUTH_MODE", "POLYMARKET_KAGGLE_AUTH_SOURCE")


class KaggleAuthenticationError(RuntimeError):
    """Authentication failed before any collection or publication was started."""


def _read_token_file(filename: str | Path, source: str) -> str | None:
    try:
        path = Path(filename)
        if not path.exists():
            return None
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise KaggleAuthenticationError(f"Cannot read Kaggle credential from {source}") from None
    return token or None


def _personal_token() -> tuple[str | None, str | None]:
    value = os.environ.get("KAGGLE_API_TOKEN", "").strip()
    if value:
        try:
            is_file = Path(value).is_file()
        except OSError:
            is_file = False
        if is_file:
            token = _read_token_file(value, "environment file")
            if not token:
                raise KaggleAuthenticationError("Kaggle environment credential file is empty")
            return token, "environment_file"
        return value, "environment"
    for filename in ("~/.kaggle/access_token", "~/.kaggle/access_token.txt"):
        token = _read_token_file(Path(filename).expanduser(), "access token file")
        if token:
            return token, "token_file"
    return None, None


def _secret_token() -> str:
    try:
        from kaggle_secrets import UserSecretsClient
        token = UserSecretsClient().get_secret("KAGGLE_API_TOKEN")
    except Exception as exc:
        # Secret-service exceptions can carry response details. Only expose
        # their type; do not propagate or chain a possibly sensitive message.
        raise KaggleAuthenticationError(
            f"Kaggle Secret KAGGLE_API_TOKEN is unavailable ({type(exc).__name__}); "
            "provide platform credentials or enable the Notebook Secret") from None
    if not isinstance(token, str) or not token.strip():
        raise KaggleAuthenticationError("Kaggle Secret KAGGLE_API_TOKEN is empty")
    return token.strip()


def verify_token(token: str, expected_owner: str = OWNER) -> str:
    """Use the CLI's read-only token introspection endpoint, with a timeout.

    This validates identity, not Dataset write permissions. In particular, a
    failed selected token must not silently fall back to another identity.
    """
    request = Request(INTROSPECT_URL, data=json.dumps({"token": token}).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                               "User-Agent": "kaggle-api/v1.7.0"},
                      method="POST")
    try:
        with urlopen(request, timeout=40) as response:
            result = json.loads(response.read(65537))
    except HTTPError as exc:
        raise KaggleAuthenticationError(f"Kaggle credential validation returned HTTP {exc.code}") from None
    except Exception as exc:
        raise KaggleAuthenticationError(
            f"Kaggle credential validation failed ({type(exc).__name__})") from None
    if not isinstance(result, dict) or result.get("active") is not True:
        raise KaggleAuthenticationError("Selected Kaggle credential is not active")
    username = result.get("username")
    if not isinstance(username, str) or username.casefold() != expected_owner.casefold():
        raise KaggleAuthenticationError("Selected Kaggle credential belongs to a different account")
    return username


@contextmanager
def kaggle_auth(mode: str = "auto", *, expected_owner: str = OWNER, verifier=None, secret_loader=None):
    """Apply one explicit source to the producer and its CLI subprocesses.

    ``auto`` discovers a platform token, then an environment/file token, and
    only then the named Kaggle Secret. Availability is checked before selection;
    validation failures never switch sources. ``personal`` excludes platform
    tokens even though kagglesdk normally gives those higher priority.
    All environment changes are restored when the producer exits.
    """
    if mode not in AUTH_MODES:
        raise KaggleAuthenticationError("Unknown Kaggle authentication mode")
    original = {name: os.environ.get(name) for name in AUTH_ENVIRONMENT}
    token = source = None
    try:
        if mode in {"auto", "platform"} and os.environ.get("KAGGLE_KERNEL_RUN_TYPE") is not None \
                and os.environ.get("KAGGLE_DATA_PROXY_URL") is not None:
            filename = os.environ.get("KAGGLE_API_V1_TOKEN")
            if filename:
                token = _read_token_file(filename, "platform")
            if token:
                source = "platform"
        if mode == "platform" and token is None:
            raise KaggleAuthenticationError("Kaggle platform credential is unavailable")
        if token is None:
            token, source = _personal_token()
        if token is None:
            token, source = (secret_loader or _secret_token)(), "secret"
            if not isinstance(token, str) or not token.strip():
                raise KaggleAuthenticationError("Kaggle Secret KAGGLE_API_TOKEN is empty")
            token = token.strip()
        if source == "platform":
            os.environ.pop("KAGGLE_API_TOKEN", None)
            sdk_source = "KAGGLE_API_V1_TOKEN"
        else:
            # The SDK checks this platform path before KAGGLE_API_TOKEN.
            os.environ.pop("KAGGLE_API_V1_TOKEN", None)
            os.environ["KAGGLE_API_TOKEN"] = token
            sdk_source = "KAGGLE_API_TOKEN"
        os.environ["POLYMARKET_KAGGLE_AUTH_MODE"] = mode
        os.environ["POLYMARKET_KAGGLE_AUTH_SOURCE"] = source
        info = {"mode": mode, "source": source, "sdk_source": sdk_source}
        print("Kaggle authentication selected: " + json.dumps(info, sort_keys=True), flush=True)
        (verifier or verify_token)(token, expected_owner)
        print("Kaggle credential identity verified", flush=True)
        yield info
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
