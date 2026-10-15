"""AWS Bedrock provider: SigV4 signing, request, and event-stream parsing."""

import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"

# Cache resolved AWS credentials per profile to avoid re-spawning the AWS CLI
# on every request. SSO/profile lookups can take 0.5–2s.
_CREDENTIALS_CACHE = {}
_CREDENTIALS_CACHE_LOCK = threading.Lock()
# How long to trust env/settings credentials before re-resolving (no expiry
# is reported for those, so we expire defensively).
_STATIC_CREDENTIALS_TTL = timedelta(minutes=15)


class BedrockHTTPError(Exception):
    """Raised when AWS Bedrock returns a non-2xx HTTP response.

    Attributes:
        status: The HTTP status code.
        message: The parsed error message from the response body.
        error_type: The Bedrock error type (e.g. "ValidationException"), if
            present in the response body.
    """

    def __init__(self, status, message, error_type=""):
        super().__init__(f"Bedrock HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.error_type = error_type


def _hmac_sha256(key, msg):
    if isinstance(msg, str):
        msg = msg.encode("utf-8")
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).digest()


def _get_signature_key(secret_key, date_stamp, region, service):
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    return k_signing


def _uri_encode_path_for_signing(path):
    """URI-encode each path segment for the SigV4 canonical request.

    Per the SigV4 spec for non-S3 services, the canonical URI is the path
    URI-encoded once, with the segment separator '/' preserved. The path
    we receive already has the model id encoded by ``_build_request_path``;
    the colons in Bedrock model ids (e.g. "...:0") survive that initial
    encode as literal '%3A' here, which is what Bedrock expects.
    """
    segments = path.split("/")
    return "/".join(urllib.parse.quote(seg, safe="") for seg in segments)


def _strip_export_prefix(line):
    """Remove a leading 'export ' from a shell variable assignment line."""
    return line[7:] if line.startswith("export ") else line


def _strip_surrounding_quotes(value):
    """Strip a single matching pair of ' or " from value, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _subprocess_kwargs_no_window(timeout=10):
    """Return subprocess.run kwargs that don't flash a console on Windows."""
    kwargs = {"capture_output": True, "text": True, "timeout": timeout}
    if sys.platform == "win32":
        # CREATE_NO_WINDOW = 0x08000000. Avoids a console flash when called
        # from a GUI process like Sublime Text.
        kwargs["creationflags"] = 0x08000000
    return kwargs


def _parse_iso8601_expiration(value):
    """Parse an ISO 8601 expiration string into a timezone-aware datetime."""
    if not value:
        return None
    # AWS returns e.g. "2026-06-12T15:00:00+00:00" with --format json. The
    # 'Z' suffix is also possible; normalise both.
    value = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _get_credentials_from_profile(profile=None):
    """Resolve credentials via the AWS CLI's export-credentials command.

    Uses ``aws configure export-credentials --format process``.

    Args:
        profile: Optional named profile. If None, the default profile is used.

    Returns:
        A dict with keys access_key, secret_key, session_token (str or None),
        and expiration (datetime or None), or None if the AWS CLI is missing
        or the profile lookup failed.
    """
    cmd = ["aws", "configure", "export-credentials", "--format", "process"]
    if profile:
        cmd.extend(["--profile", profile])
    try:
        result = subprocess.run(cmd, **_subprocess_kwargs_no_window())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return _get_credentials_from_profile_env(profile)
    access_key = data.get("AccessKeyId", "")
    secret_key = data.get("SecretAccessKey", "")
    session_token = data.get("SessionToken") or None
    expiration = _parse_iso8601_expiration(data.get("Expiration"))
    if access_key and secret_key:
        return {
            "access_key": access_key,
            "secret_key": secret_key,
            "session_token": session_token,
            "expiration": expiration,
        }
    return None


def _get_credentials_from_profile_env(profile=None):
    """Fallback: parse 'aws configure export-credentials --format env'."""
    cmd = ["aws", "configure", "export-credentials", "--format", "env"]
    if profile:
        cmd.extend(["--profile", profile])
    try:
        result = subprocess.run(cmd, **_subprocess_kwargs_no_window())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    creds = {}
    for raw in result.stdout.strip().split("\n"):
        if "=" not in raw:
            continue
        line = _strip_export_prefix(raw.strip())
        key, _, value = line.partition("=")
        creds[key.strip()] = _strip_surrounding_quotes(value.strip())
    access_key = creds.get("AWS_ACCESS_KEY_ID", "")
    secret_key = creds.get("AWS_SECRET_ACCESS_KEY", "")
    session_token = creds.get("AWS_SESSION_TOKEN") or None
    if access_key and secret_key:
        return {
            "access_key": access_key,
            "secret_key": secret_key,
            "session_token": session_token,
            "expiration": None,
        }
    return None


def _get_credentials_from_env():
    """Return AWS credentials from environment variables, or None."""
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    session_token = os.environ.get("AWS_SESSION_TOKEN") or None
    if access_key and secret_key:
        return {
            "access_key": access_key,
            "secret_key": secret_key,
            "session_token": session_token,
            "expiration": None,
        }
    return None


def _credentials_still_valid(creds):
    """Return True if cached credentials have not yet expired."""
    if not creds:
        return False
    expiration = creds.get("expiration")
    if expiration is None:
        # Static creds (env/settings) — trust for a bounded window.
        cached_at = creds.get("cached_at")
        if cached_at is None:
            return False
        return datetime.now(timezone.utc) - cached_at < _STATIC_CREDENTIALS_TTL
    # Refresh a minute early to avoid races against AWS-side expiry.
    return datetime.now(timezone.utc) + timedelta(seconds=60) < expiration


def get_aws_credentials(settings):
    """Resolve AWS credentials from settings, env vars, or AWS CLI profile.

    Resolution order:
        1. aws_access_key_id + aws_secret_access_key in settings
        2. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY environment variables
        3. ``aws_profile`` setting (via 'aws configure export-credentials')
        4. Default profile (same call, no --profile)

    Resolved credentials are cached per cache-key (settings vs env vs profile
    name) until their reported expiration, or for 15 minutes when no
    expiration is reported.

    Args:
        settings: The plugin settings object (Sublime ``Settings``).

    Returns:
        A dict with keys access_key, secret_key, session_token, and
        expiration. Or ``None`` if no credentials could be resolved.
    """
    settings_access = settings.get("aws_access_key_id", "")
    settings_secret = settings.get("aws_secret_access_key", "")
    if settings_access and settings_secret:
        cache_key = ("settings", settings_access)
        with _CREDENTIALS_CACHE_LOCK:
            cached = _CREDENTIALS_CACHE.get(cache_key)
            if _credentials_still_valid(cached):
                return cached
            session_token = settings.get("aws_session_token") or None
            creds = {
                "access_key": settings_access,
                "secret_key": settings_secret,
                "session_token": session_token,
                "expiration": None,
                "cached_at": datetime.now(timezone.utc),
            }
            _CREDENTIALS_CACHE[cache_key] = creds
            return creds

    env_access = os.environ.get("AWS_ACCESS_KEY_ID", "")
    if env_access:
        cache_key = ("env", env_access)
        with _CREDENTIALS_CACHE_LOCK:
            cached = _CREDENTIALS_CACHE.get(cache_key)
            if _credentials_still_valid(cached):
                return cached
            creds = _get_credentials_from_env()
            if creds:
                creds["cached_at"] = datetime.now(timezone.utc)
                _CREDENTIALS_CACHE[cache_key] = creds
                return creds

    profile = settings.get("aws_profile") or None
    cache_key = ("profile", profile or "__default__")
    with _CREDENTIALS_CACHE_LOCK:
        cached = _CREDENTIALS_CACHE.get(cache_key)
        if _credentials_still_valid(cached):
            return cached
        creds = _get_credentials_from_profile(profile)
        if creds:
            if creds.get("expiration") is None:
                creds["cached_at"] = datetime.now(timezone.utc)
            _CREDENTIALS_CACHE[cache_key] = creds
            return creds
    return None


def _build_request_path(model_id, streaming=False):
    """Build the URL path for the Bedrock invoke endpoint."""
    endpoint = "invoke-with-response-stream" if streaming else "invoke"
    encoded_model = urllib.parse.quote(model_id, safe="")
    return f"/model/{encoded_model}/{endpoint}"


def _parse_bedrock_error_body(body_text):
    """Extract (message, error_type) from a Bedrock error response body."""
    try:
        data = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return body_text, ""
    message = data.get("message") or data.get("Message") or body_text
    error_type = data.get("__type") or data.get("type") or ""
    # __type often looks like "com.amazon.coral...#ValidationException"; keep
    # only the last segment for readability.
    if "#" in error_type:
        error_type = error_type.split("#", 1)[1]
    return message, error_type


class _BedrockStreamResponse:
    """Tiny wrapper that owns both the http.client response and connection.

    The streaming caller only needs ``read`` (for the parser) and ``close``
    (for cleanup). Bundling the connection here means callers don't have to
    poke at private attributes to release resources.
    """

    def __init__(self, response, conn):
        self._response = response
        self._conn = conn

    def read(self, n=-1):
        return self._response.read(n)

    @property
    def fp(self):
        return self._response.fp

    def close(self):
        try:
            self._response.close()
        finally:
            self._conn.close()


def bedrock_request(
    region,
    model_id,
    body_dict,
    credentials,
    streaming=False,
    verify_ssl=True,
    timeout=30,
):
    """Make a SigV4-signed request to AWS Bedrock.

    Args:
        region: AWS region, e.g. "us-east-1".
        model_id: Bedrock model id, e.g.
            "anthropic.claude-3-5-sonnet-20241022-v2:0".
        body_dict: The request body as a JSON-serialisable dict.
        credentials: A dict with access_key, secret_key, and optional
            session_token (as returned by ``get_aws_credentials``).
        streaming: When True, hits the invoke-with-response-stream endpoint
            and returns a stream wrapper. When False, hits invoke and
            returns the parsed JSON response.
        verify_ssl: When False, disables certificate verification.
        timeout: Per-operation socket timeout in seconds.

    Returns:
        For non-streaming: the parsed JSON response (dict).
        For streaming: a ``_BedrockStreamResponse`` exposing ``read``/
        ``close``/``fp`` — pass to ``parse_event_stream``.

    Raises:
        BedrockHTTPError: On non-2xx responses.
    """
    host = f"bedrock-runtime.{region}.amazonaws.com"
    request_path = _build_request_path(model_id, streaming=streaming)
    body = json.dumps(body_dict).encode("utf-8")

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    signed_headers_dict = {
        "content-type": "application/json",
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if credentials.get("session_token"):
        signed_headers_dict["x-amz-security-token"] = (
            credentials["session_token"]
        )

    signed_header_keys = sorted(signed_headers_dict.keys())
    signed_headers_str = ";".join(signed_header_keys)
    canonical_headers = "".join(
        f"{k}:{signed_headers_dict[k]}\n" for k in signed_header_keys
    )

    canonical_uri = _uri_encode_path_for_signing(request_path)
    service = "bedrock"

    canonical_request = "\n".join([
        "POST",
        canonical_uri,
        "",  # empty query string
        canonical_headers,
        signed_headers_str,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _get_signature_key(
        credentials["secret_key"], date_stamp, region, service
    )
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={credentials['access_key']}/"
        f"{credential_scope}, SignedHeaders={signed_headers_str}, "
        f"Signature={signature}"
    )

    headers = {
        "Content-Type": "application/json",
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }
    if credentials.get("session_token"):
        headers["X-Amz-Security-Token"] = credentials["session_token"]

    if verify_ssl:
        context = ssl.create_default_context()
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    # Use http.client to avoid urllib's URL re-encoding of the colon in
    # Bedrock model ids.
    conn = http.client.HTTPSConnection(host, context=context, timeout=timeout)
    try:
        conn.request("POST", request_path, body=body, headers=headers)
        response = conn.getresponse()
    except Exception:
        conn.close()
        raise

    if response.status != 200:
        error_body = response.read().decode("utf-8", errors="replace")
        conn.close()
        message, error_type = _parse_bedrock_error_body(error_body)
        raise BedrockHTTPError(response.status, message, error_type)

    if not streaming:
        try:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
        finally:
            conn.close()

    return _BedrockStreamResponse(response, conn)


def parse_event_stream(response, should_cancel=None):
    """Parse the AWS event-stream binary format from a streaming response.

    Yields parsed JSON dicts that match the Anthropic SSE event shape, so
    callers can reuse the same handling code for both providers.

    The prelude CRC and trailing message CRC are intentionally not verified
    — this is a small, from-scratch parser and the underlying TLS connection
    already provides integrity. If you ever need stronger guarantees, switch
    to botocore's eventstream parser.

    Args:
        response: A response object exposing ``read(n)`` and (optionally)
            ``fp`` for socket access; typically the
            ``_BedrockStreamResponse`` returned by ``bedrock_request``.
        should_cancel: Optional zero-arg callable. When provided, the parser
            polls it between events and after partial reads, and returns
            early if it returns truthy. The underlying socket is set to a
            short timeout so blocking reads can be interrupted.

    Raises:
        BedrockHTTPError: When the stream contains an exception event.
        RuntimeError: If the stream ends mid-message.
    """
    buf = b""

    # Make blocking reads interruptible by cancellation polling.
    if should_cancel is not None:
        sock = getattr(getattr(response, "fp", None), "_sock", None)
        if sock is not None:
            try:
                sock.settimeout(0.5)
            except Exception:
                pass

    def cancelled():
        return should_cancel is not None and should_cancel()

    def read_exactly(n):
        nonlocal buf
        while len(buf) < n:
            if cancelled():
                return None
            try:
                chunk = response.read(n - len(buf))
            except socket.timeout:
                continue
            if not chunk:
                if buf:
                    raise RuntimeError("Unexpected end of event stream")
                return None
            buf += chunk
        result = buf[:n]
        buf = buf[n:]
        return result

    while True:
        if cancelled():
            return
        # Prelude: total_length(4) + headers_length(4) + prelude_crc(4)
        prelude_data = read_exactly(12)
        if prelude_data is None:
            return

        total_length, headers_length, _ = struct.unpack(">III", prelude_data)

        # total_length includes the 12-byte prelude.
        remaining = total_length - 12
        if remaining <= 0:
            continue
        message_data = read_exactly(remaining)
        if message_data is None:
            return

        headers_bytes = message_data[:headers_length]
        # Payload sits between headers and the trailing 4-byte message CRC.
        payload_bytes = message_data[headers_length:-4]

        headers = {}
        pos = 0
        while pos < len(headers_bytes):
            name_len = headers_bytes[pos]
            pos += 1
            if pos + name_len > len(headers_bytes):
                break
            name = headers_bytes[pos:pos + name_len].decode("utf-8")
            pos += name_len
            if pos >= len(headers_bytes):
                break
            header_type = headers_bytes[pos]
            pos += 1
            if header_type == 7:  # String type
                if pos + 2 > len(headers_bytes):
                    break
                value_len = struct.unpack(
                    ">H", headers_bytes[pos:pos + 2]
                )[0]
                pos += 2
                if pos + value_len > len(headers_bytes):
                    break
                value = headers_bytes[pos:pos + value_len].decode("utf-8")
                pos += value_len
                headers[name] = value
            else:
                break

        message_type = headers.get(":message-type", "")

        if message_type == "exception":
            error_msg = payload_bytes.decode("utf-8", errors="replace")
            message, error_type = _parse_bedrock_error_body(error_msg)
            raise BedrockHTTPError(0, message, error_type or "stream_error")

        if not payload_bytes:
            continue

        event_type = headers.get(":event-type", "")

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        # Bedrock wraps the Anthropic event in {"bytes": "<base64>"} for
        # chunk events.
        if event_type == "chunk" and "bytes" in payload:
            inner_bytes = base64.b64decode(payload["bytes"])
            try:
                yield json.loads(inner_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        elif payload:
            yield payload
