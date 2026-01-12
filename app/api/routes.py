import os
import threading
from flask import Blueprint, jsonify, request, redirect, session, current_app
from dataclasses import asdict

from app.config import PRESETS, load_upload_config, save_upload_config, UploadConfig, AppConfig
from app.services.instances import auth_service, drive_service, app_config
from app.services.files import get_files_to_upload, browse_directory, is_safe_path
from app.state import state

api = Blueprint('api', __name__, url_prefix='/api')

@api.route('/status')
def api_status():
    """Get current status"""
    creds_exist = auth_service.credentials_exist()
    authenticated = auth_service.is_authenticated()
    config = load_upload_config(app_config.CONFIG_FILE)

    return jsonify({
        'credentials_exist': creds_exist,
        'authenticated': authenticated,
        'config': config.model_dump(),
        'presets': PRESETS,
        'upload_state': state.upload.to_dict(),
        'restore_state': state.restore.to_dict()
    })

@api.route('/auth/start')
def auth_start():
    """Start OAuth flow"""
    if not auth_service.credentials_exist():
        return jsonify({'error': 'credentials.json not found'}), 400

    # Use PUBLIC_URL env var if set, otherwise try to detect from request
    public_url = app_config.PUBLIC_URL
    if public_url:
        redirect_uri = f"{public_url.rstrip('/')}/api/auth/callback"
    else:
        # Try to use X-Forwarded headers from proxy
        proto = request.headers.get('X-Forwarded-Proto', 'http')
        host = request.headers.get('X-Forwarded-Host', request.host)
        redirect_uri = f"{proto}://{host}/api/auth/callback"

    flow = auth_service.create_flow(redirect_uri)

    auth_url, oauth_state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )

    session['oauth_state'] = oauth_state
    session['redirect_uri'] = redirect_uri

    return jsonify({'auth_url': auth_url})

@api.route('/auth/callback')
def auth_callback():
    """OAuth callback"""
    if 'oauth_state' not in session:
        return redirect('/?error=invalid_state')

    returned_state = request.args.get('state')
    if returned_state != session['oauth_state']:
        return redirect('/?error=invalid_state')

    redirect_uri = session.get('redirect_uri')

    try:
        flow = auth_service.create_flow(redirect_uri)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        auth_service.save_credentials(creds)

        return redirect('/?auth=success')
    except Exception as e:
        return redirect(f'/?error={str(e)}')

@api.route('/auth/logout', methods=['POST'])
def auth_logout():
    auth_service.logout()
    return jsonify({'success': True})

@api.route('/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        config = load_upload_config(app_config.CONFIG_FILE)
        return jsonify(config.model_dump())

    try:
        data = request.json
        config = UploadConfig(**data)
        save_upload_config(config, app_config.CONFIG_FILE)
        return jsonify({'success': True, 'config': config.model_dump()})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@api.route('/preset/<preset_name>')
def api_preset(preset_name):
    if preset_name not in PRESETS:
        return jsonify({'error': 'Preset not found'}), 404
    return jsonify(PRESETS[preset_name])

@api.route('/scan', methods=['POST'])
def api_scan():
    config_file = load_upload_config(app_config.CONFIG_FILE)
    data = request.json or {}

    # Merge defaults from saved config
    source_path = data.get('source_path', config_file.source_path)
    exclusions = data.get('exclusions', config_file.exclusions)
    include_hidden = data.get('include_hidden', config_file.include_hidden)
    max_size_mb = data.get('max_file_size_mb', config_file.max_file_size_mb)

    if not os.path.exists(source_path):
        return jsonify({'error': f'Path does not exist: {source_path}'}), 400

    if not is_safe_path(source_path):
        return jsonify({'error': 'Access denied: Path is outside of /workspace'}), 403

    files = get_files_to_upload(source_path, exclusions, include_hidden, max_size_mb)

    total_size = sum(f['size'] for f in files)

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

@api.route('/upload/start', methods=['POST'])
def api_upload_start():
    if state.upload.is_running:
        return jsonify({'error': 'Upload already in progress'}), 400

    if not auth_service.is_authenticated():
        return jsonify({'error': 'Not authenticated with Google Drive'}), 401

    config = load_upload_config(app_config.CONFIG_FILE)

    # Need access to socketio to emit events from background thread
    socketio = current_app.extensions['socketio']

    thread = threading.Thread(target=drive_service.run_upload_process, args=(config, socketio))
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': 'Upload started'})

@api.route('/upload/pause', methods=['POST'])
def api_upload_pause():
    state.upload.set_paused(True)
    return jsonify({'success': True})

@api.route('/upload/resume', methods=['POST'])
def api_upload_resume():
    state.upload.set_paused(False)
    return jsonify({'success': True})

@api.route('/upload/cancel', methods=['POST'])
def api_upload_cancel():
    state.upload.request_cancel()
    return jsonify({'success': True})

@api.route('/upload/status')
def api_upload_status():
    return jsonify(state.upload.to_dict())

@api.route('/browse')
def api_browse():
    path = request.args.get('path', '/workspace')
    if not is_safe_path(path):
        return jsonify({'error': 'Access denied: Path is outside of /workspace'}), 403
    try:
        result = browse_directory(path)
        return jsonify(result)
    except Exception as e:
        status_code = 404 if 'exist' in str(e) else 403 if 'Permission' in str(e) else 400
        return jsonify({'error': str(e)}), status_code

@api.route('/drive/folders')
def api_drive_folders():
    if not auth_service.is_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401

    parent_id = request.args.get('parent_id')
    try:
        folders = drive_service.list_folders(parent_id)
        return jsonify({'folders': folders, 'parent_id': parent_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@api.route('/restore/start', methods=['POST'])
def api_restore_start():
    if state.restore.is_running:
        return jsonify({'error': 'Restore already in progress'}), 400

    if not auth_service.is_authenticated():
        return jsonify({'error': 'Not authenticated with Google Drive'}), 401

    data = request.json or {}
    folder_id = data.get('folder_id')
    folder_name = data.get('folder_name', 'restored_backup')
    dest_path = data.get('dest_path', '/workspace')
    contents_only = data.get('contents_only', False)

    if not is_safe_path(dest_path):
        return jsonify({'error': 'Access denied: Destination path is outside of /workspace'}), 403

    if not folder_id:
        return jsonify({'error': 'No folder selected'}), 400

    socketio = current_app.extensions['socketio']

    thread = threading.Thread(
        target=drive_service.run_restore_process,
        args=(folder_id, folder_name, dest_path, contents_only, socketio)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': 'Restore started'})

@api.route('/restore/pause', methods=['POST'])
def api_restore_pause():
    state.restore.set_paused(True)
    return jsonify({'success': True})

@api.route('/restore/resume', methods=['POST'])
def api_restore_resume():
    state.restore.set_paused(False)
    return jsonify({'success': True})

@api.route('/restore/cancel', methods=['POST'])
def api_restore_cancel():
    state.restore.request_cancel()
    return jsonify({'success': True})

@api.route('/restore/status')
def api_restore_status():
    return jsonify(state.restore.to_dict())
