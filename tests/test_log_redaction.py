"""Unit tests for app.log_redaction.SecretRedactingFilter."""
import logging

from app.log_redaction import SecretRedactingFilter, install_secret_redaction


def _redact(msg, *args):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    SecretRedactingFilter().filter(record)
    return record.getMessage()


def test_redacts_besttime_key_in_httpx_url():
    secret = "pri_aff50a71a038456db88864b16d9d6800"
    out = _redact(
        f'HTTP Request: POST https://besttime.app/api/v1/forecasts/live'
        f'?api_key_private={secret} "HTTP/1.1 200 OK"'
    )
    assert secret not in out
    assert "REDACTED" in out
    assert "HTTP/1.1 200 OK" in out  # surrounding content preserved


def test_redacts_key_passed_as_logging_arg():
    # httpx logs with %-args: "HTTP Request: %s %s", method, url
    secret = "pri_deadbeefdeadbeef0123456789abcd"
    out = _redact("HTTP Request: %s %s", "POST", f"https://x?api_key_private={secret}")
    assert secret not in out


def test_redacts_google_places_key():
    secret = "AIzaSyD-ExampleExampleExampleExample123"
    out = _redact(f"GET https://maps.googleapis.com/v1/places?key={secret}&fields=id")
    assert secret not in out
    assert "fields=id" in out


def test_redacts_secret_inside_params_dict_repr():
    secret = "pri_aff50a71a038456db88864b16d9d6800"
    out = _redact(
        "[BestTimeAPIClient] POST https://x params={'api_key_private': '%s'} body=None" % secret
    )
    assert secret not in out


def test_leaves_benign_key_eq_untouched():
    msg = "processing key=venue_id for nearby lookup"
    assert _redact(msg) == msg


def test_install_is_idempotent():
    lg = logging.getLogger("test_log_redaction_install")
    handler = logging.StreamHandler()
    lg.addHandler(handler)
    install_secret_redaction(lg)
    install_secret_redaction(lg)
    assert sum(isinstance(f, SecretRedactingFilter) for f in handler.filters) == 1
