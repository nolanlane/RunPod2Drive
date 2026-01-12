import os
import threading
from typing import Optional
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request

# Scopes
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def find_credentials_file(default_name: str = 'credentials.json') -> str:
    """Look for credentials.json in multiple locations"""
    locations = [
        default_name,
        f'/workspace/{default_name}',
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), default_name), # Repo root
        os.path.expanduser(f'~/{default_name}'),
    ]
    for loc in locations:
        if os.path.exists(loc):
            return loc
    return default_name

class AuthService:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = find_credentials_file(credentials_file)
        self.token_file = token_file
        self._lock = threading.Lock()

    def get_credentials(self) -> Optional[Credentials]:
        """Get valid credentials from token file"""
        if os.path.exists(self.token_file):
            try:
                # Load credentials
                creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

                if creds and creds.valid:
                    return creds

                # Check expiry and refresh if possible
                if creds and creds.expired and creds.refresh_token:
                    # Lock only the refresh and write operation to prevent race conditions
                    with self._lock:
                        # Reload to ensure we don't refresh a token that was just refreshed by another thread
                        creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
                        if creds.valid:
                            return creds

                        creds.refresh(Request())
                        with open(self.token_file, 'w') as token:
                            token.write(creds.to_json())
                    return creds
            except Exception:
                pass
        return None

    def is_authenticated(self) -> bool:
        """Check if user is authenticated with Google Drive"""
        return self.get_credentials() is not None

    def create_flow(self, redirect_uri: str) -> Flow:
        return Flow.from_client_secrets_file(
            self.credentials_file,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )

    def save_credentials(self, creds: Credentials):
        with self._lock:
            with open(self.token_file, 'w') as token:
                token.write(creds.to_json())

    def logout(self):
        with self._lock:
            if os.path.exists(self.token_file):
                os.remove(self.token_file)

    def credentials_exist(self) -> bool:
        return os.path.exists(self.credentials_file)
