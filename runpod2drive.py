#!/usr/bin/env python3
"""
RunPod2Drive - Upload RunPod filesystem to Google Drive
A web-based tool for uploading files from a RunPod instance to Google Drive
with configurable exclusions, presets, and progress tracking.
"""

import argparse
import json
import os
import sys
import threading
import time
import fnmatch
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
import mimetypes
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue

# Allow OAuth over HTTP for development (RunPod internal network)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_socketio import SocketIO, emit
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Configuration
SCOPES = ['https://www.googleapis.com/auth/drive.file']
TOKEN_FILE = 'token.json'
CONFIG_FILE = 'config.json'

# Look for credentials.json in multiple locations
def find_credentials_file():
    locations = [
        'credentials.json',
        '/workspace/credentials.json',
        os.path.join(os.path.dirname(__file__), 'credentials.json'),
        os.path.expanduser('~/credentials.json'),
    ]
    for loc in locations:
        if os.path.exists(loc):
            return loc
    return 'credentials.json'  # Default, will show warning if not found

CREDENTIALS_FILE = find_credentials_file()

# Upload settings
CHUNK_SIZE = 25 * 1024 * 1024  # 25MB chunks
MAX_WORKERS = 3  # Reduced parallel threads to avoid API issues

# Cache for folder IDs to avoid repeated lookups
folder_cache = {}
folder_cache_lock = threading.Lock()

# Global state
upload_state = {
    'is_running': False,
    'is_paused': False,
    'current_file': '',
    'total_files': 0,
    'processed_files': 0,
    'total_bytes': 0,
    'uploaded_bytes': 0,
    'errors': [],
    'start_time': None,
    'cancel_requested': False
}

upload_lock = threading.Lock()


@dataclass
class UploadConfig:
    """Configuration for upload settings"""
    source_path: str = '/workspace'
    drive_folder_name: str = 'RunPod_Backup'
    exclusions: List[str] = None
    include_hidden: bool = False
    max_file_size_mb: int = 0  # 0 = no limit
    preset: str = 'default'
    
    def __post_init__(self):
        if self.exclusions is None:
            self.exclusions = []


# Preset configurations
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


def load_config() -> UploadConfig:
    """Load configuration from file"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                return UploadConfig(**data)
        except Exception:
            pass
    return UploadConfig()


def save_config(config: UploadConfig):
    """Save configuration to file"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(asdict(config), f, indent=2)


def get_credentials() -> Optional[Credentials]:
    """Get valid credentials from token file"""
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            if creds and creds.valid:
                return creds
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
                return creds
        except Exception:
            pass
    return None


def is_authenticated() -> bool:
    """Check if user is authenticated with Google Drive"""
    return get_credentials() is not None


def should_exclude(file_path: str, exclusions: List[str], include_hidden: bool) -> bool:
    """Check if a file should be excluded based on patterns"""
    name = os.path.basename(file_path)
    
    # Check hidden files
    if not include_hidden and name.startswith('.'):
        return True
    
    # Check exclusion patterns
    for pattern in exclusions:
        if fnmatch.fnmatch(name, pattern):
            return True
        if fnmatch.fnmatch(file_path, pattern):
            return True
        # Check if any parent directory matches
        parts = Path(file_path).parts
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    
    return False


def get_files_to_upload(source_path: str, exclusions: List[str], 
                        include_hidden: bool, max_size_mb: int) -> List[Dict]:
    """Get list of files to upload with their info"""
    files = []
    source_path = os.path.abspath(source_path)
    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else float('inf')
    
    for root, dirs, filenames in os.walk(source_path):
        # Filter directories in-place to avoid walking into excluded dirs
        dirs[:] = [d for d in dirs if not should_exclude(
            os.path.join(root, d), exclusions, include_hidden
        )]
        
        for filename in filenames:
            file_path = os.path.join(root, filename)
            
            if should_exclude(file_path, exclusions, include_hidden):
                continue
            
            try:
                stat = os.stat(file_path)
                if stat.st_size > max_size_bytes:
                    continue
                
                rel_path = os.path.relpath(file_path, source_path)
                files.append({
                    'path': file_path,
                    'rel_path': rel_path,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
            except (OSError, PermissionError):
                continue
    
    return files


def get_or_create_folder(service, folder_name: str, parent_id: str = None) -> str:
    """Get or create a folder in Google Drive (thread-safe with caching)"""
    cache_key = f"{parent_id}:{folder_name}"
    
    with folder_cache_lock:
        if cache_key in folder_cache:
            return folder_cache[cache_key]
    
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])
    
    if items:
        with folder_cache_lock:
            folder_cache[cache_key] = items[0]['id']
        return items[0]['id']
    
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    if parent_id:
        file_metadata['parents'] = [parent_id]
    
    folder = service.files().create(body=file_metadata, fields='id').execute()
    with folder_cache_lock:
        folder_cache[cache_key] = folder.get('id')
    return folder.get('id')


def upload_single_file(service, file_info: Dict, root_folder_id: str) -> Dict:
    """Upload a single file to Google Drive. Returns result dict."""
    file_path = file_info['path']
    rel_path = file_info['rel_path']
    file_size = file_info['size']
    
    try:
        # Get parent folder (should be cached from prebuild)
        parts = Path(rel_path).parts
        parent_id = root_folder_id
        for folder_name in parts[:-1]:
            parent_id = get_or_create_folder(service, folder_name, parent_id)
        
        file_name = parts[-1]
        mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        
        file_metadata = {
            'name': file_name,
            'parents': [parent_id]
        }
        
        if file_size == 0:
            # Handle empty files - just create
            service.files().create(body=file_metadata, fields='id').execute()
        else:
            # Upload with large chunks, no existence check (faster)
            media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True, chunksize=CHUNK_SIZE)
            request = service.files().create(body=file_metadata, media_body=media, fields='id')
            
            response = None
            while response is None:
                if upload_state['cancel_requested']:
                    return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Cancelled'}
                while upload_state['is_paused'] and not upload_state['cancel_requested']:
                    time.sleep(0.1)
                status, response = request.next_chunk()
        
        return {'success': True, 'file': rel_path, 'size': file_size, 'error': None}
    
    except Exception as e:
        return {'success': False, 'file': rel_path, 'size': file_size, 'error': str(e)}


def run_upload(config: UploadConfig):
    """Main upload function with parallel processing"""
    global upload_state, folder_cache
    
    try:
        creds = get_credentials()
        if not creds:
            upload_state['errors'].append("Not authenticated with Google Drive")
            upload_state['is_running'] = False
            socketio.emit('upload_error', {'message': 'Not authenticated'})
            return
        
        # Clear folder cache for fresh upload
        folder_cache.clear()
        
        service = build('drive', 'v3', credentials=creds)
        
        # Get files to upload
        socketio.emit('upload_status', {'message': 'Scanning files...'})
        files = get_files_to_upload(
            config.source_path,
            config.exclusions,
            config.include_hidden,
            config.max_file_size_mb
        )
        
        if not files:
            upload_state['is_running'] = False
            socketio.emit('upload_complete', {'message': 'No files to upload'})
            return
        
        upload_state['total_files'] = len(files)
        upload_state['total_bytes'] = sum(f['size'] for f in files)
        upload_state['start_time'] = time.time()
        
        socketio.emit('upload_started', {
            'total_files': upload_state['total_files'],
            'total_bytes': upload_state['total_bytes']
        })
        
        # Create root folder
        root_folder_id = get_or_create_folder(service, config.drive_folder_name)
        
        socketio.emit('upload_status', {'message': f'Uploading {len(files)} files with {MAX_WORKERS} parallel workers...'})
        
        # Sort files: smaller files first for faster initial progress
        files_sorted = sorted(files, key=lambda x: x['size'])
        
        # Progress update function
        def emit_progress():
            socketio.emit('upload_progress', {
                'current_file': upload_state['current_file'],
                'processed_files': upload_state['processed_files'],
                'total_files': upload_state['total_files'],
                'uploaded_bytes': upload_state['uploaded_bytes'],
                'total_bytes': upload_state['total_bytes']
            })
        
        # Upload files in parallel
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Each worker gets its own service instance
            def worker_upload(file_info):
                worker_creds = get_credentials()
                worker_service = build('drive', 'v3', credentials=worker_creds)
                return upload_single_file(worker_service, file_info, root_folder_id)
            
            # Submit all files
            future_to_file = {executor.submit(worker_upload, f): f for f in files_sorted}
            
            # Process completed uploads
            for future in as_completed(future_to_file):
                if upload_state['cancel_requested']:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                
                result = future.result()
                
                with upload_lock:
                    upload_state['processed_files'] += 1
                    upload_state['current_file'] = result['file']
                    
                    if result['success']:
                        upload_state['uploaded_bytes'] += result['size']
                    else:
                        if result['error'] != 'Cancelled':
                            error_msg = f"Error uploading {result['file']}: {result['error']}"
                            upload_state['errors'].append(error_msg)
                            socketio.emit('upload_error', {'message': error_msg, 'file': result['file']})
                
                emit_progress()
        
        # Complete
        elapsed = time.time() - upload_state['start_time']
        socketio.emit('upload_complete', {
            'processed_files': upload_state['processed_files'],
            'total_files': upload_state['total_files'],
            'uploaded_bytes': upload_state['uploaded_bytes'],
            'elapsed_seconds': elapsed,
            'errors': upload_state['errors'],
            'cancelled': upload_state['cancel_requested']
        })
        
    except Exception as e:
        socketio.emit('upload_error', {'message': f'Upload failed: {str(e)}'})
    finally:
        upload_state['is_running'] = False
        upload_state['cancel_requested'] = False


# Flask Routes
@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Get current status"""
    creds_exist = os.path.exists(CREDENTIALS_FILE)
    authenticated = is_authenticated()
    config = load_config()
    
    return jsonify({
        'credentials_exist': creds_exist,
        'authenticated': authenticated,
        'config': asdict(config),
        'presets': PRESETS,
        'upload_state': upload_state
    })


@app.route('/api/auth/start')
def auth_start():
    """Start OAuth flow"""
    if not os.path.exists(CREDENTIALS_FILE):
        return jsonify({'error': 'credentials.json not found'}), 400
    
    # Use PUBLIC_URL env var if set, otherwise try to detect from request
    public_url = os.environ.get('PUBLIC_URL', '').rstrip('/')
    if public_url:
        redirect_uri = f"{public_url}/api/auth/callback"
    else:
        # Try to use X-Forwarded headers from proxy
        proto = request.headers.get('X-Forwarded-Proto', 'http')
        host = request.headers.get('X-Forwarded-Host', request.host)
        redirect_uri = f"{proto}://{host}/api/auth/callback"
    
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    
    session['oauth_state'] = state
    session['redirect_uri'] = redirect_uri
    
    return jsonify({'auth_url': auth_url})


@app.route('/api/auth/callback')
def auth_callback():
    """OAuth callback"""
    if 'oauth_state' not in session:
        return redirect('/?error=invalid_state')
    
    # Extract state from request and validate it
    returned_state = request.args.get('state')
    
    if returned_state != session['oauth_state']:
        return redirect('/?error=invalid_state')
    
    redirect_uri = session.get('redirect_uri')
    
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
        
        return redirect('/?auth=success')
    except Exception as e:
        return redirect(f'/?error={str(e)}')


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """Logout and remove token"""
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    return jsonify({'success': True})


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update configuration"""
    if request.method == 'GET':
        config = load_config()
        return jsonify(asdict(config))
    
    data = request.json
    config = UploadConfig(
        source_path=data.get('source_path', '/workspace'),
        drive_folder_name=data.get('drive_folder_name', 'RunPod_Backup'),
        exclusions=data.get('exclusions', []),
        include_hidden=data.get('include_hidden', False),
        max_file_size_mb=data.get('max_file_size_mb', 0),
        preset=data.get('preset', 'default')
    )
    save_config(config)
    return jsonify({'success': True, 'config': asdict(config)})


@app.route('/api/preset/<preset_name>')
def api_preset(preset_name):
    """Get preset configuration"""
    if preset_name not in PRESETS:
        return jsonify({'error': 'Preset not found'}), 404
    return jsonify(PRESETS[preset_name])


@app.route('/api/scan', methods=['POST'])
def api_scan():
    """Scan source directory and return file statistics"""
    config = load_config()
    data = request.json or {}
    
    source_path = data.get('source_path', config.source_path)
    exclusions = data.get('exclusions', config.exclusions)
    include_hidden = data.get('include_hidden', config.include_hidden)
    max_size_mb = data.get('max_file_size_mb', config.max_file_size_mb)
    
    if not os.path.exists(source_path):
        return jsonify({'error': f'Path does not exist: {source_path}'}), 400
    
    files = get_files_to_upload(source_path, exclusions, include_hidden, max_size_mb)
    
    total_size = sum(f['size'] for f in files)
    
    # Get file type breakdown
    extensions = {}
    for f in files:
        ext = os.path.splitext(f['rel_path'])[1].lower() or '(no extension)'
        if ext not in extensions:
            extensions[ext] = {'count': 0, 'size': 0}
        extensions[ext]['count'] += 1
        extensions[ext]['size'] += f['size']
    
    return jsonify({
        'total_files': len(files),
        'total_size': total_size,
        'extensions': extensions,
        'sample_files': [f['rel_path'] for f in files[:20]]
    })


@app.route('/api/upload/start', methods=['POST'])
def api_upload_start():
    """Start upload process"""
    global upload_state
    
    if upload_state['is_running']:
        return jsonify({'error': 'Upload already in progress'}), 400
    
    if not is_authenticated():
        return jsonify({'error': 'Not authenticated with Google Drive'}), 401
    
    config = load_config()
    
    # Reset state
    upload_state = {
        'is_running': True,
        'is_paused': False,
        'current_file': '',
        'total_files': 0,
        'processed_files': 0,
        'total_bytes': 0,
        'uploaded_bytes': 0,
        'errors': [],
        'start_time': None,
        'cancel_requested': False
    }
    
    # Start upload in background thread
    thread = threading.Thread(target=run_upload, args=(config,))
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'message': 'Upload started'})


@app.route('/api/upload/pause', methods=['POST'])
def api_upload_pause():
    """Pause upload"""
    global upload_state
    upload_state['is_paused'] = True
    return jsonify({'success': True})


@app.route('/api/upload/resume', methods=['POST'])
def api_upload_resume():
    """Resume upload"""
    global upload_state
    upload_state['is_paused'] = False
    return jsonify({'success': True})


@app.route('/api/upload/cancel', methods=['POST'])
def api_upload_cancel():
    """Cancel upload"""
    global upload_state
    upload_state['cancel_requested'] = True
    return jsonify({'success': True})


@app.route('/api/upload/status')
def api_upload_status():
    """Get upload status"""
    return jsonify(upload_state)


@app.route('/api/browse')
def api_browse():
    """Browse filesystem directories"""
    path = request.args.get('path', '/workspace')
    
    if not os.path.exists(path):
        return jsonify({'error': 'Path does not exist'}), 404
    
    if not os.path.isdir(path):
        return jsonify({'error': 'Not a directory'}), 400
    
    items = []
    try:
        for name in sorted(os.listdir(path)):
            full_path = os.path.join(path, name)
            is_dir = os.path.isdir(full_path)
            items.append({
                'name': name,
                'path': full_path,
                'is_dir': is_dir
            })
    except PermissionError:
        return jsonify({'error': 'Permission denied'}), 403
    
    parent = os.path.dirname(path) if path != '/' else None
    
    return jsonify({
        'current': path,
        'parent': parent,
        'items': items
    })


def main():
    parser = argparse.ArgumentParser(description='RunPod2Drive - Upload files to Google Drive')
    parser.add_argument('-p', '--port', type=int, default=7860, 
                        help='Port to run the web UI (default: 7860)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--debug', action='store_true',
                        help='Run in debug mode')
    
    args = parser.parse_args()
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                      RunPod2Drive                            ║
║          Upload your RunPod files to Google Drive            ║
╠══════════════════════════════════════════════════════════════╣
║  Starting server on http://{args.host}:{args.port}                    ║
║                                                              ║
║  Make sure credentials.json is in the same directory.       ║
╚══════════════════════════════════════════════════════════════╝
""")
    
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"⚠️  Warning: {CREDENTIALS_FILE} not found!")
        print("   Please add your Google OAuth credentials file.")
        print("   Get it from: https://console.cloud.google.com/apis/credentials")
    
    socketio.run(app, host=args.host, port=args.port, debug=args.debug, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
