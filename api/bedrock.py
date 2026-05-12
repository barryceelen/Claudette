import base64
import hashlib
import hmac
import http.client
import json
import os
import struct
import subprocess
import ssl
import urllib.parse
from datetime import datetime, timezone

BEDROCK_ANTHROPIC_VERSION = 'bedrock-2023-05-31'


def _hmac_sha256(key, msg):
    if isinstance(msg, str):
        msg = msg.encode('utf-8')
    if isinstance(key, str):
        key = key.encode('utf-8')
    return hmac.new(key, msg, hashlib.sha256).digest()


def _get_signature_key(secret_key, date_stamp, region, service):
    k_date = _hmac_sha256(('AWS4' + secret_key).encode('utf-8'), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, 'aws4_request')
    return k_signing


def _uri_encode_path_for_signing(path):
    """Double-encode the path for SigV4 canonical request."""
    segments = path.split('/')
    return '/'.join(urllib.parse.quote(seg, safe='') for seg in segments)


def _get_credentials_from_profile(profile=None):
    """Get AWS credentials from CLI profile using 'aws configure export-credentials'."""
    cmd = ['aws', 'configure', 'export-credentials', '--format', 'env']
    if profile:
        cmd.extend(['--profile', profile])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return None
        creds = {}
        for line in result.stdout.strip().split('\n'):
            if '=' in line:
                line = line.replace('export ', '')
                key, _, value = line.partition('=')
                creds[key.strip()] = value.strip()
        access_key = creds.get('AWS_ACCESS_KEY_ID', '')
        secret_key = creds.get('AWS_SECRET_ACCESS_KEY', '')
        session_token = creds.get('AWS_SESSION_TOKEN', '')
        if access_key and secret_key:
            return {
                'access_key': access_key,
                'secret_key': secret_key,
                'session_token': session_token
            }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _get_credentials_from_env():
    """Get AWS credentials from environment variables."""
    access_key = os.environ.get('AWS_ACCESS_KEY_ID', '')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
    session_token = os.environ.get('AWS_SESSION_TOKEN', '')
    if access_key and secret_key:
        return {
            'access_key': access_key,
            'secret_key': secret_key,
            'session_token': session_token
        }
    return None


def get_aws_credentials(settings):
    """Resolve AWS credentials from settings, env vars, or AWS CLI profile."""
    access_key = settings.get('aws_access_key_id', '')
    secret_key = settings.get('aws_secret_access_key', '')
    session_token = settings.get('aws_session_token', '')
    if access_key and secret_key:
        return {
            'access_key': access_key,
            'secret_key': secret_key,
            'session_token': session_token
        }
    creds = _get_credentials_from_env()
    if creds:
        return creds
    profile = settings.get('aws_profile', '')
    return _get_credentials_from_profile(profile or None)


def _build_request_path(model_id, streaming=False):
    """Build the URL path for the Bedrock invoke endpoint."""
    endpoint = 'invoke-with-response-stream' if streaming else 'invoke'
    encoded_model = urllib.parse.quote(model_id, safe='')
    return '/model/{0}/{1}'.format(encoded_model, endpoint)


def bedrock_request(region, model_id, body_dict, credentials, streaming=False, verify_ssl=True):
    """
    Make a signed request to AWS Bedrock and return the response.
    For non-streaming: returns parsed JSON dict.
    For streaming: returns the http.client.HTTPResponse (caller must read and close).
    """
    host = 'bedrock-runtime.{0}.amazonaws.com'.format(region)
    request_path = _build_request_path(model_id, streaming=streaming)
    body = json.dumps(body_dict).encode('utf-8')

    now = datetime.now(timezone.utc)
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = now.strftime('%Y%m%d')
    payload_hash = hashlib.sha256(body).hexdigest()

    # Headers to sign
    signed_headers_dict = {
        'content-type': 'application/json',
        'host': host,
        'x-amz-content-sha256': payload_hash,
        'x-amz-date': amz_date,
    }
    if credentials.get('session_token'):
        signed_headers_dict['x-amz-security-token'] = credentials['session_token']

    signed_header_keys = sorted(signed_headers_dict.keys())
    signed_headers_str = ';'.join(signed_header_keys)
    canonical_headers = ''.join(
        '{0}:{1}\n'.format(k, signed_headers_dict[k]) for k in signed_header_keys
    )

    # SigV4 requires double-encoding the path in the canonical request
    canonical_uri = _uri_encode_path_for_signing(request_path)
    service = 'bedrock'

    canonical_request = '\n'.join([
        'POST',
        canonical_uri,
        '',  # empty query string
        canonical_headers,
        signed_headers_str,
        payload_hash
    ])

    credential_scope = '{0}/{1}/{2}/aws4_request'.format(date_stamp, region, service)
    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256',
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()
    ])

    signing_key = _get_signature_key(
        credentials['secret_key'], date_stamp, region, service
    )
    signature = hmac.new(
        signing_key, string_to_sign.encode('utf-8'), hashlib.sha256
    ).hexdigest()

    authorization = (
        'AWS4-HMAC-SHA256 Credential={0}/{1}, SignedHeaders={2}, Signature={3}'
    ).format(credentials['access_key'], credential_scope, signed_headers_str, signature)

    # Build final headers for the HTTP request
    headers = {
        'Content-Type': 'application/json',
        'Host': host,
        'X-Amz-Date': amz_date,
        'X-Amz-Content-Sha256': payload_hash,
        'Authorization': authorization,
    }
    if credentials.get('session_token'):
        headers['X-Amz-Security-Token'] = credentials['session_token']

    # Use http.client to avoid urllib's URL re-encoding
    if verify_ssl:
        context = ssl.create_default_context()
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    conn = http.client.HTTPSConnection(host, context=context)
    conn.request('POST', request_path, body=body, headers=headers)
    response = conn.getresponse()

    if response.status != 200:
        error_body = response.read().decode('utf-8', errors='replace')
        conn.close()
        try:
            error_data = json.loads(error_body)
            msg = error_data.get('message', error_body)
        except (json.JSONDecodeError, KeyError):
            msg = error_body
        raise RuntimeError("Bedrock HTTP {0}: {1}".format(response.status, msg))

    if not streaming:
        raw = response.read().decode('utf-8')
        conn.close()
        return json.loads(raw)

    # For streaming, return the response object — caller manages reading
    # Attach the connection so caller can close it
    response._conn = conn
    return response


def parse_event_stream(response):
    """
    Parse AWS event stream binary format from a Bedrock streaming response.
    Yields parsed JSON dicts matching the Anthropic SSE event format.
    """
    buf = b''

    def read_exactly(n):
        nonlocal buf
        while len(buf) < n:
            chunk = response.read(n - len(buf))
            if not chunk:
                if buf:
                    raise RuntimeError("Unexpected end of event stream")
                return None
            buf += chunk
        result = buf[:n]
        buf = buf[n:]
        return result

    while True:
        # Read prelude: total_length(4) + headers_length(4) + prelude_crc(4)
        prelude_data = read_exactly(12)
        if prelude_data is None:
            return

        total_length, headers_length, _ = struct.unpack('>III', prelude_data)

        # Read rest of message (total_length includes the 12-byte prelude)
        remaining = total_length - 12
        if remaining <= 0:
            continue
        message_data = read_exactly(remaining)
        if message_data is None:
            return

        # Parse headers
        headers_bytes = message_data[:headers_length]
        # Payload is between headers and message CRC (last 4 bytes)
        payload_bytes = message_data[headers_length:-4]

        # Parse event stream headers (name-value pairs with type byte)
        headers = {}
        pos = 0
        while pos < len(headers_bytes):
            if pos >= len(headers_bytes):
                break
            name_len = headers_bytes[pos]
            pos += 1
            if pos + name_len > len(headers_bytes):
                break
            name = headers_bytes[pos:pos + name_len].decode('utf-8')
            pos += name_len
            if pos >= len(headers_bytes):
                break
            header_type = headers_bytes[pos]
            pos += 1
            if header_type == 7:  # String type
                if pos + 2 > len(headers_bytes):
                    break
                value_len = struct.unpack('>H', headers_bytes[pos:pos + 2])[0]
                pos += 2
                if pos + value_len > len(headers_bytes):
                    break
                value = headers_bytes[pos:pos + value_len].decode('utf-8')
                pos += value_len
                headers[name] = value
            else:
                break

        # Check for exceptions
        message_type = headers.get(':message-type', '')

        if message_type == 'exception':
            error_msg = payload_bytes.decode('utf-8', errors='replace')
            try:
                error_data = json.loads(error_msg)
                raise RuntimeError("Bedrock stream error: {0}".format(
                    error_data.get('message', error_msg)
                ))
            except json.JSONDecodeError:
                raise RuntimeError("Bedrock stream error: {0}".format(error_msg))

        if not payload_bytes:
            continue

        event_type = headers.get(':event-type', '')

        try:
            payload = json.loads(payload_bytes.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        # Bedrock wraps the Anthropic event in {"bytes": "<base64>"} for chunk events
        if event_type == 'chunk' and 'bytes' in payload:
            inner_bytes = base64.b64decode(payload['bytes'])
            try:
                yield json.loads(inner_bytes.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        elif payload:
            yield payload
