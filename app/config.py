import json
import os
import secrets
from pathlib import Path
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

def get_secret_key() -> str:
    """
    Get or create a persistent secret key.
    1. Check for .secret_key file
    2. If missing, generate new key and save it
    3. Return key
    """
    secret_file = Path('.secret_key')
    try:
        if secret_file.exists():
            key = secret_file.read_text().strip()
            if key:
                return key

        # Generate secure random key
        key = secrets.token_hex(32)
        try:
            # Try to save for persistence
            secret_file.write_text(key)
        except OSError:
            # If write fails (e.g. read-only fs), return key anyway
            pass
        return key
    except Exception:
        # Fallback for any other error
        return secrets.token_hex(32)

# Presets configuration
PRESETS = {
    'default': {
        'name': 'Default',
        'description': 'Standard backup excluding common unnecessary files',
        'exclusions': [
            '*.pyc', '__pycache__', '.git', '.gitignore',
            'node_modules', '.npm', '.cache',
            '*.log', '*.tmp', '*.temp',
            '.DS_Store', 'Thumbs.db',
            '*.egg-info', '.eggs', 'dist', 'build',
            '.pytest_cache', '.mypy_cache',
            'venv', '.venv', 'env', '.env'
        ],
        'include_hidden': False
    },
    'ml_training': {
        'name': 'ML Training',
        'description': 'Optimized for machine learning projects - keeps models and checkpoints',
        'exclusions': [
            '*.pyc', '__pycache__', '.git',
            'node_modules', '.npm',
            '*.log', '*.tmp',
            '.DS_Store', 'Thumbs.db',
            '.pytest_cache', '.mypy_cache',
            'venv', '.venv',
            'wandb', '.wandb',
            # Keep: .pt, .pth, .ckpt, .safetensors, .bin model files
        ],
        'include_hidden': False
    },
    'full_backup': {
        'name': 'Full Backup',
        'description': 'Backup everything including hidden files',
        'exclusions': [],
        'include_hidden': True
    },
    'code_only': {
        'name': 'Code Only',
        'description': 'Only source code files',
        'exclusions': [
            '*.pyc', '__pycache__', '.git', '.gitignore',
            'node_modules', '.npm', '.cache',
            '*.log', '*.tmp', '*.temp',
            '.DS_Store', 'Thumbs.db',
            '*.egg-info', '.eggs', 'dist', 'build',
            '.pytest_cache', '.mypy_cache',
            'venv', '.venv', 'env', '.env',
            '*.pt', '*.pth', '*.ckpt', '*.safetensors', '*.bin',
            '*.h5', '*.hdf5', '*.pkl', '*.pickle',
            '*.zip', '*.tar', '*.gz', '*.rar',
            '*.mp4', '*.avi', '*.mov', '*.mkv',
            '*.jpg', '*.jpeg', '*.png', '*.gif', '*.bmp',
            '*.wav', '*.mp3', '*.flac'
        ],
        'include_hidden': False
    },
    'minimal': {
        'name': 'Minimal',
        'description': 'Only essential files, excludes large binaries and media',
        'exclusions': [
            '*.pyc', '__pycache__', '.git', '.gitignore',
            'node_modules', '.npm', '.cache',
            '*.log', '*.tmp', '*.temp',
            '.DS_Store', 'Thumbs.db',
            '*.egg-info', '.eggs', 'dist', 'build',
            '.pytest_cache', '.mypy_cache',
            'venv', '.venv', 'env', '.env',
            '*.pt', '*.pth', '*.ckpt', '*.safetensors',
            '*.bin', '*.h5', '*.hdf5',
            '*.zip', '*.tar', '*.gz', '*.rar', '*.7z',
            '*.mp4', '*.avi', '*.mov', '*.mkv', '*.webm',
            '*.jpg', '*.jpeg', '*.png', '*.gif', '*.bmp', '*.webp',
            '*.wav', '*.mp3', '*.flac', '*.ogg',
            '*.so', '*.dll', '*.dylib',
            '*.o', '*.obj', '*.a'
        ],
        'include_hidden': False
    }
}

class UploadConfig(BaseModel):
    """Configuration for upload settings"""
    source_path: str = Field(default='/workspace')
    drive_folder_name: str = Field(default='RunPod_Backup')
    exclusions: List[str] = Field(default_factory=list)
    include_hidden: bool = Field(default=False)
    max_file_size_mb: int = Field(default=0)  # 0 = no limit
    preset: str = Field(default='default')
    max_workers: int = Field(default=8, ge=1, le=15)  # 1-15 workers, default 8

class AppConfig(BaseSettings):
    """Application level configuration from environment variables"""
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )
    
    # Use default_factory to generate a secure key if not provided in env
    SECRET_KEY: str = Field(default_factory=get_secret_key)
    OAUTHLIB_INSECURE_TRANSPORT: str = Field(default='1')  # Allow OAuth over HTTP for dev
    PUBLIC_URL: Optional[str] = None

    # Path settings
    CREDENTIALS_FILE: str = 'credentials.json'
    TOKEN_FILE: str = 'token.json'
    CONFIG_FILE: str = 'config.json'

def load_upload_config(config_file: str) -> UploadConfig:
    """Load configuration from file"""
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
                return UploadConfig(**data)
        except Exception:
            pass
    return UploadConfig()

def save_upload_config(config: UploadConfig, config_file: str):
    """Save configuration to file"""
    with open(config_file, 'w') as f:
        f.write(config.model_dump_json(indent=2))
