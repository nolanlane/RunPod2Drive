from app.config import AppConfig
from app.services.auth import AuthService
from app.services.drive import DriveService

# Load app config
app_config = AppConfig()

# Initialize services
auth_service = AuthService(
    credentials_file=app_config.CREDENTIALS_FILE,
    token_file=app_config.TOKEN_FILE
)

drive_service = DriveService(credentials_provider=auth_service)
