"""The data lake writer must not require static AWS credentials.

In production cs-server authenticates with the EC2 INSTANCE ROLE, which boto3
resolves through its default credential chain. Passing explicit keys is a
local-development escape hatch only. Regressing this — e.g. copying the menu
photo client, which hard-requires keys — would put a long-lived secret back into
the container environment.
"""
from unittest.mock import patch

from app.dao.datalake_writer import _build_s3_client


class TestCredentialChain:
    def test_no_keys_configured_defers_to_the_default_chain(self):
        with patch("boto3.client") as mock_client:
            _build_s3_client(region="us-east-1", access_key_id=None, secret_access_key=None)

        _, kwargs = mock_client.call_args
        assert "aws_access_key_id" not in kwargs
        assert "aws_secret_access_key" not in kwargs
        assert kwargs["region_name"] == "us-east-1"

    def test_empty_strings_also_defer_to_the_default_chain(self):
        with patch("boto3.client") as mock_client:
            _build_s3_client(region="us-east-1", access_key_id="", secret_access_key="")

        _, kwargs = mock_client.call_args
        assert "aws_access_key_id" not in kwargs

    def test_explicit_keys_are_honored_for_local_development(self):
        with patch("boto3.client") as mock_client:
            _build_s3_client(
                region="us-east-1", access_key_id="AKIA_LOCAL", secret_access_key="shh"
            )

        _, kwargs = mock_client.call_args
        assert kwargs["aws_access_key_id"] == "AKIA_LOCAL"
        assert kwargs["aws_secret_access_key"] == "shh"


class TestTimeouts:
    def test_bounds_every_upload_so_a_slow_s3_cannot_stall_the_flusher(self):
        with patch("boto3.client") as mock_client:
            _build_s3_client(region="us-east-1", access_key_id=None, secret_access_key=None)

        config = mock_client.call_args.kwargs["config"]
        assert config.connect_timeout == 5
        assert config.read_timeout == 5
        assert config.retries["max_attempts"] == 2
