"""
Unit tests for Gmail API client integration.
Tests various OAuth authentication scenarios including token refresh and reauthorization.
"""

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Try to import the Gmail module, skip tests if dependencies are missing
try:
    from tools import gmail
    import config
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import Resource

    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False


@pytest.fixture
def mock_credentials():
    """Create mock Credentials object for testing."""
    creds = Mock(spec=Credentials)
    creds.valid = True
    creds.expired = False
    creds.refresh_token = "mock-refresh-token"
    creds.to_json.return_value = '{"token": "mock-token"}'
    return creds


@pytest.fixture
def expired_credentials():
    """Create mock expired Credentials object for testing."""
    creds = Mock(spec=Credentials)
    creds.valid = False
    creds.expired = True
    creds.refresh_token = "mock-refresh-token"
    creds.to_json.return_value = '{"token": "mock-token"}'
    return creds


@pytest.fixture
def mock_flow():
    """Create mock InstalledAppFlow for testing."""
    flow = Mock()
    mock_cred_result = Mock(spec=Credentials)
    mock_cred_result.to_json.return_value = '{"token": "mock-token"}'
    flow.run_local_server.return_value = mock_cred_result
    return flow


class TestGetGmailService:
    """Tests for the get_gmail_service function."""

    @pytest.mark.skipif(not GMAIL_AVAILABLE, reason="Gmail dependencies not installed")
    def test_valid_existing_token(self, mock_credentials, tmp_path):
        """Test that a valid existing token is reused."""
        # Setup
        token_file = tmp_path / "token.json"
        creds_file = tmp_path / "credentials.json"
        # Write a valid client secrets structure
        creds_file.write_text('{"installed": {"client_id": "test-client-id", "client_secret": "test-client-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}')
        # Write a valid token
        token_file.write_text('{"token": "test-token", "refresh_token": "test-refresh-token", "token_uri": "https://oauth2.googleapis.com/token", "client_id": "test-client-id", "client_secret": "test-client-secret", "scopes": ["https://www.googleapis.com/auth/gmail.send"]}')

        # Store original values and patch config
        original_token_file = config.TOKEN_FILE
        original_creds_file = config.CREDENTIALS_FILE

        try:
            config.TOKEN_FILE = token_file
            config.CREDENTIALS_FILE = creds_file

            # Mock Credentials.from_authorized_user_file to return our mock
            with patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=mock_credentials) as mock_from_file:
                with patch('tools.gmail.build') as mock_build:
                    mock_build.return_value = Mock(spec=Resource)

                    # Execute
                    service = gmail.get_gmail_service()

                    # Verify
                    mock_build.assert_called_once_with("gmail", "v1", credentials=mock_credentials)
                    # Should not have tried to refresh or initiate flow
                    assert not mock_credentials.refresh.called
        finally:
            # Restore original values
            config.TOKEN_FILE = original_token_file
            config.CREDENTIALS_FILE = original_creds_file

    @pytest.mark.skipif(not GMAIL_AVAILABLE, reason="Gmail dependencies not installed")
    def test_expired_token_successful_refresh(self, expired_credentials, tmp_path):
        """Test that an expired token is successfully refreshed."""
        # Setup
        token_file = tmp_path / "token.json"
        creds_file = tmp_path / "credentials.json"
        # Write a valid client secrets structure
        creds_file.write_text('{"installed": {"client_id": "test-client-id", "client_secret": "test-client-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}')
        # Write an expired token
        token_file.write_text('{"token": "expired-token", "refresh_token": "test-refresh-token", "token_uri": "https://oauth2.googleapis.com/token", "client_id": "test-client-id", "client_secret": "test-client-secret", "scopes": ["https://www.googleapis.com/auth/gmail.send"], "expiry": "2020-01-01T00:00:00Z"}')

        # Store original values and patch config
        original_token_file = config.TOKEN_FILE
        original_creds_file = config.CREDENTIALS_FILE

        try:
            config.TOKEN_FILE = token_file
            config.CREDENTIALS_FILE = creds_file

            # Mock Credentials.from_authorized_user_file to return expired creds
            with patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=expired_credentials):
                expired_credentials.refresh.side_effect = lambda _: setattr(
                    expired_credentials, "valid", True
                )
                with patch('tools.gmail.Request') as mock_request:
                    mock_request_instance = Mock()
                    mock_request.return_value = mock_request_instance

                    with patch(
                        'tools.gmail.InstalledAppFlow.from_client_secrets_file',
                        side_effect=AssertionError("Successful refresh must not launch OAuth."),
                    ):
                        with patch('tools.gmail.build') as mock_build:
                            mock_build.return_value = Mock(spec=Resource)

                            # Execute
                            service = gmail.get_gmail_service()

                            # Verify
                            expired_credentials.refresh.assert_called_once_with(mock_request_instance)
                            mock_build.assert_called_once_with("gmail", "v1", credentials=expired_credentials)
                            # Token should have been saved
                            assert token_file.exists()
        finally:
            # Restore original values
            config.TOKEN_FILE = original_token_file
            config.CREDENTIALS_FILE = original_creds_file

    @pytest.mark.skipif(not GMAIL_AVAILABLE, reason="Gmail dependencies not installed")
    def test_invalid_grant_triggers_reauthorization(self, tmp_path):
        """Test that invalid_grant error triggers fresh OAuth flow."""
        # Setup
        token_file = tmp_path / "token.json"
        creds_file = tmp_path / "credentials.json"
        # Write a valid client secrets structure
        creds_file.write_text('{"installed": {"client_id": "test-client-id", "client_secret": "test-client-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}')
        # Write an expired token (to trigger refresh attempt)
        token_file.write_text('{"token": "expired-token", "refresh_token": "test-refresh-token", "token_uri": "https://oauth2.googleapis.com/token", "client_id": "test-client-id", "client_secret": "test-client-secret", "scopes": ["https://www.googleapis.com/auth/gmail.send"], "expiry": "2020-01-01T00:00:00Z"}')

        # Store original values and patch config
        original_token_file = config.TOKEN_FILE
        original_creds_file = config.CREDENTIALS_FILE

        try:
            config.TOKEN_FILE = token_file
            config.CREDENTIALS_FILE = creds_file

            # Mock Credentials.from_authorized_user_file to return expired creds
            mock_creds = Mock(spec=Credentials)
            mock_creds.valid = False
            mock_creds.expired = True
            mock_creds.refresh_token = "mock-refresh-token"
            mock_creds.to_json.return_value = '{"token": "mock-token"}'

            with patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=mock_creds):
                # Mock refresh to raise invalid_grant error
                with patch.object(mock_creds, 'refresh', side_effect=Exception("invalid_grant: Token has been expired or revoked.")):
                    # Mock the OAuth flow
                    mock_flow = Mock()
                    mock_cred_result = Mock(spec=Credentials)
                    mock_cred_result.to_json.return_value = '{"token": "new-mock-token"}'
                    mock_flow.run_local_server.return_value = mock_cred_result

                    with patch('tools.gmail.InstalledAppFlow.from_client_secrets_file', return_value=mock_flow):
                        with patch('tools.gmail.build') as mock_build:
                            mock_build.return_value = Mock(spec=Resource)

                            # Execute
                            service = gmail.get_gmail_service()

                            # Verify
                            mock_creds.refresh.assert_called_once()  # Refresh was attempted
                            mock_flow.run_local_server.assert_called_once()  # Flow was initiated
                            mock_build.assert_called_once()  # Service was built with new credentials
                            # Token should have been saved
                            assert token_file.exists()
        finally:
            # Restore original values
            config.TOKEN_FILE = original_token_file
            config.CREDENTIALS_FILE = original_creds_file

    @pytest.mark.skipif(not GMAIL_AVAILABLE, reason="Gmail dependencies not installed")
    def test_missing_token_triggers_authorization(self, tmp_path):
        """Test that missing token triggers OAuth flow."""
        # Setup
        token_file = tmp_path / "token.json"  # This file doesn't exist
        creds_file = tmp_path / "credentials.json"
        # Write a valid client secrets structure
        creds_file.write_text('{"installed": {"client_id": "test-client-id", "client_secret": "test-client-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}')

        # Store original values and patch config
        original_token_file = config.TOKEN_FILE
        original_creds_file = config.CREDENTIALS_FILE

        try:
            config.TOKEN_FILE = token_file
            config.CREDENTIALS_FILE = creds_file

            # Ensure token file doesn't exist
            assert not token_file.exists()

            # Mock the OAuth flow
            mock_flow = Mock()
            mock_cred_result = Mock(spec=Credentials)
            mock_cred_result.to_json.return_value = '{"token": "new-mock-token"}'
            mock_flow.run_local_server.return_value = mock_cred_result

            with patch('tools.gmail.InstalledAppFlow.from_client_secrets_file', return_value=mock_flow):
                with patch('tools.gmail.build') as mock_build:
                    mock_build.return_value = Mock(spec=Resource)

                    # Execute
                    service = gmail.get_gmail_service()

                    # Verify
                    mock_flow.run_local_server.assert_called_once()  # Flow was initiated
                    mock_build.assert_called_once()  # Service was built with new credentials
                    # Token should have been saved
                    assert token_file.exists()
        finally:
            # Restore original values
            config.TOKEN_FILE = original_token_file
            config.CREDENTIALS_FILE = original_creds_file

    @pytest.mark.skipif(not GMAIL_AVAILABLE, reason="Gmail dependencies not installed")
    def test_missing_credentials_json_raises_error(self, tmp_path):
        """Test that missing credentials.json raises a clear error."""
        # Setup
        token_file = tmp_path / "token.json"  # This file doesn't exist
        creds_file = tmp_path / "credentials.json"  # This file doesn't exist either

        # Store original values and patch config
        original_token_file = config.TOKEN_FILE
        original_creds_file = config.CREDENTIALS_FILE

        try:
            config.TOKEN_FILE = token_file
            config.CREDENTIALS_FILE = creds_file

            # Ensure neither file exists
            assert not token_file.exists()
            assert not creds_file.exists()

            # Execute and verify
            with pytest.raises(FileNotFoundError, match="Gmail credentials not found"):
                gmail.get_gmail_service()
        finally:
            # Restore original values
            config.TOKEN_FILE = original_token_file
            config.CREDENTIALS_FILE = original_creds_file

    @pytest.mark.skipif(not GMAIL_AVAILABLE, reason="Gmail dependencies not installed")
    def test_unrelated_refresh_failure_preserved(self, tmp_path):
        """Test that unrelated refresh failures (network errors) are not swallowed."""
        # Setup
        token_file = tmp_path / "token.json"
        creds_file = tmp_path / "credentials.json"
        # Write a valid client secrets structure
        creds_file.write_text('{"installed": {"client_id": "test-client-id", "client_secret": "test-client-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}')
        # Write an expired token (to trigger refresh attempt)
        token_file.write_text('{"token": "expired-token", "refresh_token": "test-refresh-token", "token_uri": "https://oauth2.googleapis.com/token", "client_id": "test-client-id", "client_secret": "test-client-secret", "scopes": ["https://www.googleapis.com/auth/gmail.send"], "expiry": "2020-01-01T00:00:00Z"}')

        # Store original values and patch config
        original_token_file = config.TOKEN_FILE
        original_creds_file = config.CREDENTIALS_FILE

        try:
            config.TOKEN_FILE = token_file
            config.CREDENTIALS_FILE = creds_file

            # Mock Credentials.from_authorized_user_file to return expired creds
            mock_creds = Mock(spec=Credentials)
            mock_creds.valid = False
            mock_creds.expired = True
            mock_creds.refresh_token = "mock-refresh-token"
            mock_creds.to_json.return_value = '{"token": "mock-token"}'

            with patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=mock_creds):
                # Mock refresh to raise a network-related error (not invalid_grant)
                with patch.object(mock_creds, 'refresh', side_effect=Exception("Network error: Connection timeout")):
                    # Execute and verify the exception is propagated
                    with pytest.raises(Exception, match="Network error: Connection timeout"):
                        gmail.get_gmail_service()

                    # Verify refresh was attempted but no flow was initiated due to the exception
                    mock_creds.refresh.assert_called_once()
        finally:
            # Restore original values
            config.TOKEN_FILE = original_token_file
            config.CREDENTIALS_FILE = original_creds_file