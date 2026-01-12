import os
import threading
import time
import mimetypes
from pathlib import Path
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials

from app.state import state
from app.services.files import get_files_to_upload
from app.config import UploadConfig

# Constants
CHUNK_SIZE = 25 * 1024 * 1024  # 25MB chunks
DEFAULT_MAX_WORKERS = int(os.getenv('MAX_WORKERS', '8'))  # Can be overridden by env var

# Google Workspace export formats
WORKSPACE_EXPORT_FORMATS = {
    'application/vnd.google-apps.document': ('application/pdf', '.pdf'),
    'application/vnd.google-apps.spreadsheet': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
    'application/vnd.google-apps.presentation': ('application/pdf', '.pdf'),
    'application/vnd.google-apps.drawing': ('image/png', '.png'),
}

class DriveService:
    def __init__(self, credentials_provider):
        self.credentials_provider = credentials_provider
        self.folder_cache = {}
        self.folder_cache_lock = threading.Lock()
        self._thread_local = threading.local()

    def get_service(self):
        # Use thread-local storage to reuse service instances across files in the same thread
        if not hasattr(self._thread_local, 'service'):
            creds = self.credentials_provider.get_credentials()
            if not creds:
                raise Exception("Not authenticated")
            self._thread_local.service = build('drive', 'v3', credentials=creds)
        return self._thread_local.service

    def get_or_create_folder(self, service, folder_name: str, parent_id: str = None) -> str:
        """Get or create a folder in Google Drive (thread-safe with caching)"""
        cache_key = f"{parent_id}:{folder_name}"

        with self.folder_cache_lock:
            # Check cache first
            if cache_key in self.folder_cache:
                return self.folder_cache[cache_key]

            # If not in cache, check Drive (within lock to prevent duplicate creation)
            query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            if parent_id:
                query += f" and '{parent_id}' in parents"
            else:
                query += " and 'root' in parents"

            results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
            items = results.get('files', [])

            if items:
                folder_id = items[0]['id']
                self.folder_cache[cache_key] = folder_id
                return folder_id

            # Create if not found
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            if parent_id:
                file_metadata['parents'] = [parent_id]

            folder = service.files().create(body=file_metadata, fields='id').execute()
            folder_id = folder.get('id')
            self.folder_cache[cache_key] = folder_id
            return folder_id

    def list_folders(self, parent_id: str = None) -> List[Dict]:
        service = self.get_service()
        query = "mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        else:
            query += " and 'root' in parents"

        folders = []
        page_token = None

        while True:
            results = service.files().list(
                q=query,
                spaces='drive',
                fields='nextPageToken, files(id, name, modifiedTime)',
                orderBy='name',
                pageSize=1000,
                pageToken=page_token
            ).execute()

            folders.extend(results.get('files', []))
            page_token = results.get('nextPageToken')

            if not page_token:
                break

        return folders

    def upload_single_file(self, service, file_info: Dict, root_folder_id: str) -> Dict:
        """Upload a single file to Google Drive. Returns result dict."""
        file_path = file_info['path']
        rel_path = file_info['rel_path']
        file_size = file_info['size']

        try:
            # Get parent folder
            parts = Path(rel_path).parts
            parent_id = root_folder_id
            for folder_name in parts[:-1]:
                parent_id = self.get_or_create_folder(service, folder_name, parent_id)

            file_name = parts[-1]
            mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'

            file_metadata = {
                'name': file_name,
                'parents': [parent_id]
            }

            if file_size == 0:
                service.files().create(body=file_metadata, fields='id').execute()
            else:
                media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True, chunksize=CHUNK_SIZE)
                request = service.files().create(body=file_metadata, media_body=media, fields='id')

                response = None
                while response is None:
                    if state.upload.cancel_requested:
                        return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Cancelled'}
                    while state.upload.is_paused and not state.upload.cancel_requested:
                        time.sleep(0.1)
                    status, response = request.next_chunk()

            return {'success': True, 'file': rel_path, 'size': file_size, 'error': None}

        except Exception as e:
            return {'success': False, 'file': rel_path, 'size': file_size, 'error': str(e)}

    def run_upload_process(self, config: UploadConfig, socketio):
        """Background process for uploading"""
        try:
            # Check auth
            if not self.credentials_provider.is_authenticated():
                state.upload.errors.append("Not authenticated with Google Drive")
                state.upload.stop()
                socketio.emit('upload_error', {'message': 'Not authenticated'})
                return

            self.folder_cache.clear()
            service = self.get_service()

            socketio.emit('upload_status', {'message': 'Scanning files...'})
            files = get_files_to_upload(
                config.source_path,
                config.exclusions,
                config.include_hidden,
                config.max_file_size_mb
            )

            if not files:
                state.upload.stop()
                socketio.emit('upload_complete', {'message': 'No files to upload'})
                return

            total_size = sum(f['size'] for f in files)
            state.upload.start(len(files), total_size)

            socketio.emit('upload_started', {
                'total_files': state.upload.total_files,
                'total_bytes': state.upload.total_bytes
            })

            root_folder_id = self.get_or_create_folder(service, config.drive_folder_name)
            socketio.emit('upload_status', {'message': f'Uploading {len(files)} files...'})

            files_sorted = sorted(files, key=lambda x: x['size'])

            last_emit_time = 0
            def emit_progress(force=False):
                nonlocal last_emit_time
                now = time.time()
                if force or now - last_emit_time > 0.5:
                    socketio.emit('upload_progress', state.upload.to_dict())
                    last_emit_time = now

            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                def worker_upload(file_info):
                    worker_service = self.get_service()
                    return self.upload_single_file(worker_service, file_info, root_folder_id)

                future_to_file = {executor.submit(worker_upload, f): f for f in files_sorted}

                for future in as_completed(future_to_file):
                    if state.upload.cancel_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    result = future.result()

                    if result['success']:
                        state.upload.update_progress(result['file'], bytes_processed=result['size'], file_completed=True)
                    else:
                        state.upload.update_progress(result['file'], file_completed=True, error=f"Error: {result['error']}")
                        if result['error'] != 'Cancelled':
                            socketio.emit('upload_error', {'message': f"Error {result['file']}: {result['error']}", 'file': result['file']})

                    emit_progress()

            # Finalize
            emit_progress(force=True)
            elapsed = time.time() - (state.upload.start_time or time.time())
            socketio.emit('upload_complete', {
                'processed_files': state.upload.processed_files,
                'uploaded_bytes': state.upload.processed_bytes,
                'elapsed_seconds': elapsed,
                'errors': state.upload.errors,
                'cancelled': state.upload.cancel_requested
            })

        except Exception as e:
            socketio.emit('upload_error', {'message': f'Upload failed: {str(e)}'})
        finally:
            state.upload.stop()

    def list_drive_files_recursive(self, service, folder_id: str, path: str = '') -> List[Dict]:
        """Recursively list all files in a Drive folder with pagination"""
        files = []
        page_token = None

        while True:
            # List files in current folder
            query = f"'{folder_id}' in parents and trashed=false"
            results = service.files().list(
                q=query,
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType, size)',
                pageSize=1000,
                pageToken=page_token
            ).execute()

            for item in results.get('files', []):
                mime_type = item['mimeType']
                item_path = os.path.join(path, item['name']) if path else item['name']

                if mime_type == 'application/vnd.google-apps.folder':
                    # Recurse into subfolder
                    files.extend(self.list_drive_files_recursive(service, item['id'], item_path))
                elif mime_type in WORKSPACE_EXPORT_FORMATS:
                    # Google Workspace file - needs export
                    export_mime, ext = WORKSPACE_EXPORT_FORMATS[mime_type]
                    files.append({
                        'id': item['id'],
                        'name': item['name'],
                        'path': item_path + ext,
                        'size': 0,  # Size unknown for Workspace files
                        'export_mime': export_mime
                    })
                elif mime_type.startswith('application/vnd.google-apps.'):
                    continue
                else:
                    files.append({
                        'id': item['id'],
                        'name': item['name'],
                        'path': item_path,
                        'size': int(item.get('size', 0)),
                        'export_mime': None
                    })

            page_token = results.get('nextPageToken')
            if not page_token:
                break

        return files

    def download_single_file(self, service, file_info: Dict, dest_path: str, progress_state=None) -> Dict:
        """Download a single file from Google Drive"""
        file_id = file_info['id']
        rel_path = file_info['path']
        file_size = file_info['size']
        export_mime = file_info.get('export_mime')
        
        # Default to restore state for backward compatibility
        if progress_state is None:
            progress_state = state.restore

        try:
            full_path = os.path.join(dest_path, rel_path)
            parent_dir = os.path.dirname(full_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            if export_mime:
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            else:
                request = service.files().get_media(fileId=file_id)

            with open(full_path, 'wb') as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    if progress_state.cancel_requested:
                        return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Cancelled'}
                    while progress_state.is_paused and not progress_state.cancel_requested:
                        time.sleep(0.1)
                    status, done = downloader.next_chunk()

            # Get actual file size after download
            actual_size = os.path.getsize(full_path) if os.path.exists(full_path) else file_size
            return {'success': True, 'file': rel_path, 'size': actual_size, 'error': None}

        except Exception as e:
            return {'success': False, 'file': rel_path, 'size': file_size, 'error': str(e)}

    def run_restore_process(self, folder_id: str, folder_name: str, dest_path: str, contents_only: bool, socketio):
        try:
            if not self.credentials_provider.is_authenticated():
                state.restore.errors.append("Not authenticated")
                state.restore.stop()
                socketio.emit('restore_error', {'message': 'Not authenticated'})
                return

            # Load config to get max_workers setting
            from app.config import load_upload_config, AppConfig
            app_config = AppConfig()
            config = load_upload_config(app_config.CONFIG_FILE)
            
            service = self.get_service()

            socketio.emit('restore_status', {'message': 'Scanning Drive folder...'})
            files = self.list_drive_files_recursive(service, folder_id)

            if not files:
                state.restore.stop()
                socketio.emit('restore_complete', {'message': 'No files to restore'})
                return

            total_bytes = sum(f['size'] for f in files if not f.get('export_mime'))
            state.restore.start(len(files), total_bytes)

            socketio.emit('restore_started', {
                'total_files': state.restore.total_files,
                'total_bytes': state.restore.total_bytes
            })

            if contents_only:
                full_dest = dest_path
            else:
                full_dest = os.path.join(dest_path, folder_name)
            os.makedirs(full_dest, exist_ok=True)

            socketio.emit('restore_status', {'message': f'Downloading {len(files)} files...'})

            last_emit_time = 0
            def emit_progress(force=False):
                nonlocal last_emit_time
                now = time.time()
                if force or now - last_emit_time > 0.5:
                    socketio.emit('restore_progress', state.restore.to_dict())
                    last_emit_time = now

            # Parallel download with ThreadPoolExecutor
            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                def worker_download(file_info):
                    state.restore.update_progress(file_info['path'])
                    worker_service = self.get_service()
                    return self.download_single_file(worker_service, file_info, full_dest, state.restore)

                future_to_file = {executor.submit(worker_download, f): f for f in files}

                for future in as_completed(future_to_file):
                    if state.restore.cancel_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    result = future.result()
                    file_info = future_to_file[future]

                    if result['success']:
                        if not file_info.get('export_mime'):
                            state.restore.update_progress(result['file'], bytes_processed=result['size'], file_completed=True)
                        else:
                            state.restore.update_progress(result['file'], file_completed=True)
                    else:
                        state.restore.update_progress(result['file'], file_completed=True, error=result['error'])
                        if result['error'] != 'Cancelled':
                            socketio.emit('restore_error', {'message': f"Error {result['file']}: {result['error']}", 'file': result['file']})

                    emit_progress()

            emit_progress(force=True)
            elapsed = time.time() - (state.restore.start_time or time.time())
            socketio.emit('restore_complete', {
                'processed_files': state.restore.processed_files,
                'downloaded_bytes': state.restore.processed_bytes,
                'elapsed_seconds': elapsed,
                'errors': state.restore.errors,
                'cancelled': state.restore.cancel_requested,
                'dest_path': full_dest
            })

        except Exception as e:
            socketio.emit('restore_error', {'message': f'Restore failed: {str(e)}'})
        finally:
            state.restore.stop()

    def run_transfer_process(self, items: list, dest_path: str, socketio):
        """Transfer selected files/folders from Google Drive to local destination"""
        try:
            state.transfer.start(0, 0)
            if not self.credentials_provider.is_authenticated():
                state.transfer.errors.append("Not authenticated")
                state.transfer.stop()
                socketio.emit('transfer_error', {'message': 'Not authenticated'})
                return

            # Load config to get max_workers setting
            from app.config import load_upload_config, AppConfig
            app_config = AppConfig()
            config = load_upload_config(app_config.CONFIG_FILE)
            
            service = self.get_service()

            socketio.emit('transfer_status', {'message': 'Scanning selected items...'})
            
            # Collect all files from selected items
            all_files = []
            for item in items:
                if item.get('type') == 'folder':
                    # Recursively list files in folder
                    folder_files = self.list_drive_files_recursive(service, item['id'])
                    all_files.extend(folder_files)
                else:
                    # Single file - get its metadata
                    try:
                        file_meta = service.files().get(
                            fileId=item['id'],
                            fields='id,name,mimeType,size'
                        ).execute()
                        
                        file_name = file_meta['name']
                        mime_type = file_meta['mimeType']
                        export_mime = None
                        
                        # Handle Google Workspace files
                        if mime_type in WORKSPACE_EXPORT_FORMATS:
                            export_mime, ext = WORKSPACE_EXPORT_FORMATS[mime_type]
                            if not file_name.endswith(ext):
                                file_name += ext
                        
                        all_files.append({
                            'id': file_meta['id'],
                            'name': file_name,
                            'path': file_name,
                            'mime_type': mime_type,
                            'size': int(file_meta.get('size', 0)),
                            'export_mime': export_mime
                        })
                    except Exception as e:
                        state.transfer.errors.append(f"Failed to get metadata for {item['name']}: {str(e)}")
            
            total_files = len(all_files)
            total_bytes = sum(f.get('size', 0) for f in all_files)
            state.transfer.update_total(total_files, total_bytes)

            socketio.emit('transfer_started', {
                'total_files': total_files,
                'total_bytes': total_bytes
            })

            last_emit_time = 0
            def emit_progress(force=False):
                nonlocal last_emit_time
                now = time.time()
                if force or now - last_emit_time > 0.5:
                    socketio.emit('transfer_progress', state.transfer.to_dict())
                    last_emit_time = now

            # Parallel download with ThreadPoolExecutor
            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                def worker_download(file_info):
                    state.transfer.update_progress(file_info['path'])
                    worker_service = self.get_service()
                    return self.download_single_file(worker_service, file_info, dest_path, state.transfer)

                future_to_file = {executor.submit(worker_download, f): f for f in all_files}

                for future in as_completed(future_to_file):
                    if state.transfer.cancel_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    result = future.result()
                    file_info = future_to_file[future]

                    if result['success']:
                        if not file_info.get('export_mime'):
                            state.transfer.update_progress(result['file'], bytes_processed=result['size'], file_completed=True)
                        else:
                            state.transfer.update_progress(result['file'], file_completed=True)
                    else:
                        state.transfer.update_progress(result['file'], file_completed=True, error=result['error'])
                        if result['error'] != 'Cancelled':
                            socketio.emit('transfer_error', {'message': f"Error {result['file']}: {result['error']}", 'file': result['file']})

                    emit_progress()

            emit_progress(force=True)
            elapsed = time.time() - (state.transfer.start_time or time.time())
            socketio.emit('transfer_complete', {
                'processed_files': state.transfer.processed_files,
                'downloaded_bytes': state.transfer.processed_bytes,
                'elapsed_seconds': elapsed,
                'errors': state.transfer.errors,
                'cancelled': state.transfer.cancel_requested,
                'dest_path': dest_path
            })

        except Exception as e:
            socketio.emit('transfer_error', {'message': f'Transfer failed: {str(e)}'})
        finally:
            state.transfer.stop()
