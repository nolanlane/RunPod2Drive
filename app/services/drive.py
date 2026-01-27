import os
import threading
import time
import random
import mimetypes
import hashlib
from pathlib import Path
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials

from app.state import state
from app.services.files import get_files_to_upload, is_safe_path
from app.config import UploadConfig

# Constants
CHUNK_SIZE = 25 * 1024 * 1024  # 25MB chunks
RESUMABLE_THRESHOLD = 5 * 1024 * 1024  # Use simple upload for files < 5MB
MAX_RETRIES = 5
INITIAL_BACKOFF = 1  # seconds
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
        self._folder_locks = {}
        self._folder_lock_refs = {}
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
        cache_key = f"{parent_id or 'root'}:{folder_name}"

        # Quick cache check with minimal lock time
        with self.folder_cache_lock:
            if cache_key in self.folder_cache:
                return self.folder_cache[cache_key]
            # Get or create per-key lock for this folder
            if cache_key not in self._folder_locks:
                self._folder_locks[cache_key] = threading.Lock()
                self._folder_lock_refs[cache_key] = 0
            self._folder_lock_refs[cache_key] += 1
            key_lock = self._folder_locks[cache_key]

        try:
            # Per-key lock allows parallel creation of different folders
            with key_lock:
                # Double-check cache after acquiring per-key lock
                with self.folder_cache_lock:
                    if cache_key in self.folder_cache:
                        return self.folder_cache[cache_key]

                # API calls outside the global lock
                # Escape single quotes in folder name for query
                escaped_name = folder_name.replace("'", "''")
                query = f"name='{escaped_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
                if parent_id:
                    query += f" and '{parent_id}' in parents"
                else:
                    query += " and 'root' in parents"

                results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
                items = results.get('files', [])

                if items:
                    folder_id = items[0]['id']
                    with self.folder_cache_lock:
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
                with self.folder_cache_lock:
                    self.folder_cache[cache_key] = folder_id
                return folder_id
        finally:
            # Cleanup: remove lock when no longer needed
            with self.folder_cache_lock:
                if cache_key in self._folder_lock_refs:
                    self._folder_lock_refs[cache_key] -= 1
                    # Only remove if ref count is 0 AND it's the same lock we used
                    if self._folder_lock_refs[cache_key] == 0 and self._folder_locks.get(cache_key) is key_lock:
                        self._folder_locks.pop(cache_key, None)
                        self._folder_lock_refs.pop(cache_key, None)

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

    def _worker_create_folder(self, parent_id, folder_name):
        """Worker task for creating folders"""
        service = self.get_service()
        return self.get_or_create_folder(service, folder_name, parent_id)

    def _worker_upload(self, file_info, root_folder_id):
        """Worker task for uploading files"""
        service = self.get_service()
        return self.upload_single_file(service, file_info, root_folder_id)

    def _worker_download(self, file_info, dest_path, progress_state):
        """Worker task for downloading files"""
        progress_state.update_progress(file_info['path'])
        service = self.get_service()
        return self.download_single_file(service, file_info, dest_path, progress_state)

    def upload_single_file(self, service, file_info: Dict, root_folder_id: str) -> Dict:
        """Upload a single file to Google Drive. Returns result dict."""
        file_path = file_info['path']
        rel_path = file_info['rel_path']
        file_size = file_info['size']

        try:
            # Check if file still exists and hasn't changed
            if not os.path.exists(file_path):
                return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'File no longer exists'}
            
            current_size = os.path.getsize(file_path)
            if current_size != file_size:
                return {'success': False, 'file': rel_path, 'size': file_size, 'error': f'File size changed ({file_size} -> {current_size})'}

            # Get parent folder
            parts = Path(rel_path).parts
            parent_id = root_folder_id
            for folder_name in parts[:-1]:
                parent_id = self.get_or_create_folder(service, folder_name, parent_id)

            file_name = parts[-1]
            mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'

            # Check for existing file with same name (for update instead of duplicate)
            escaped_name = file_name.replace("'", "''")
            existing_query = f"name='{escaped_name}' and '{parent_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
            existing = service.files().list(q=existing_query, spaces='drive', fields='files(id)').execute()
            existing_files = existing.get('files', [])

            file_metadata = {'name': file_name}
            
            # If file exists, update it; otherwise create with parent
            if existing_files:
                file_id = existing_files[0]['id']
                update_mode = True
            else:
                file_metadata['parents'] = [parent_id]
                file_id = None
                update_mode = False

            if file_size == 0:
                if update_mode:
                    response = service.files().update(fileId=file_id, body=file_metadata, fields='id, md5Checksum').execute()
                else:
                    response = service.files().create(body=file_metadata, fields='id, md5Checksum').execute()
            elif file_size < RESUMABLE_THRESHOLD:
                # Simple upload with retry logic
                media = MediaFileUpload(file_path, mimetype=mime_type, resumable=False)
                retries = 0
                while True:
                    try:
                        if update_mode:
                            response = service.files().update(fileId=file_id, media_body=media, fields='id, md5Checksum').execute()
                        else:
                            response = service.files().create(body=file_metadata, media_body=media, fields='id, md5Checksum').execute()
                        break
                    except Exception as e:
                        retries += 1
                        if retries > MAX_RETRIES:
                            raise e
                        sleep_time = (INITIAL_BACKOFF * (2 ** (retries - 1))) + random.random()
                        time.sleep(sleep_time)
            else:
                # Resumable upload for larger files
                media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True, chunksize=CHUNK_SIZE)
                if update_mode:
                    request = service.files().update(fileId=file_id, media_body=media, fields='id, md5Checksum')
                else:
                    request = service.files().create(body=file_metadata, media_body=media, fields='id, md5Checksum')

                response = None
                retries = 0
                while response is None:
                    if state.upload.cancel_requested:
                        return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Cancelled'}
                    while state.upload.is_paused and not state.upload.cancel_requested:
                        time.sleep(0.1)
                    
                    try:
                        status, response = request.next_chunk()
                        retries = 0  # Reset retries on success
                    except Exception as e:
                        retries += 1
                        if retries > MAX_RETRIES:
                            raise e
                        
                        # Exponential backoff with jitter
                        sleep_time = (INITIAL_BACKOFF * (2 ** (retries - 1))) + random.random()
                        time.sleep(sleep_time)
                        continue

            return {'success': True, 'file': rel_path, 'size': file_size, 'error': None}

        except Exception as e:
            return {'success': False, 'file': rel_path, 'size': file_size, 'error': str(e)}

    def _list_folder_children(self, folder_id: str) -> List[Dict]:
        """List immediate child folders of a folder (with pagination)"""
        service = self.get_service()
        children = []
        page_token = None
        while True:
            query = f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = service.files().list(
                q=query,
                spaces='drive',
                fields='nextPageToken, files(id, name)',
                pageSize=1000,
                pageToken=page_token
            ).execute()
            children.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        return children

    def list_folders_recursive(self, service, folder_id: str, current_path_parts: tuple = (), max_workers: int = None) -> Dict[tuple, str]:
        """List all folders using parallel breadth-first traversal"""
        found_folders = {}
        # Queue: list of (folder_id, path_parts) to process
        current_level = [(folder_id, current_path_parts)]
        
        if max_workers is None:
            max_workers = DEFAULT_MAX_WORKERS
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while current_level:
                next_level = []

                # Batch submissions to cap in-flight futures and avoid memory spikes
                batch_size = max_workers * 10
                for i in range(0, len(current_level), batch_size):
                    batch = current_level[i:i + batch_size]
                    future_to_info = {
                        executor.submit(self._list_folder_children, fid): (fid, path_parts)
                        for fid, path_parts in batch
                    }
                    
                    for future in as_completed(future_to_info):
                        parent_id, parent_path = future_to_info[future]
                        try:
                            children = future.result()
                            for child in children:
                                child_path = parent_path + (child['name'],)
                                found_folders[child_path] = child['id']
                                next_level.append((child['id'], child_path))
                        except Exception:
                            pass  # Skip folders we can't access
                
                current_level = next_level
        
        return found_folders

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

            # Pre-create folder structure to avoid bottlenecks in parallel workers
            socketio.emit('upload_status', {'message': 'Preparing folder structure...'})
            root_folder_id = self.get_or_create_folder(service, config.drive_folder_name)
            
            # Prefetch existing structure
            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS
            existing_structure = self.list_folders_recursive(service, root_folder_id, max_workers=max_workers)
            with self.folder_cache_lock:
                # The keys in existing_structure are paths (tuple of parts)
                # We need to map these to (parent_id:name) -> id for the get_or_create_folder cache
                
                # First, map the root folder's direct children
                # Then for each folder, we know its children.
                
                # Helper to map paths to IDs
                path_to_id = {(): root_folder_id}
                path_to_id.update(existing_structure)
                
                for path_parts, folder_id in existing_structure.items():
                    parent_path = path_parts[:-1]
                    name = path_parts[-1]
                    if parent_path in path_to_id:
                        parent_id = path_to_id[parent_path]
                        cache_key = f"{parent_id}:{name}"
                        self.folder_cache[cache_key] = folder_id

            unique_folders = set()
            for f in files:
                rel_path = f['rel_path']
                parts = Path(rel_path).parts
                if len(parts) > 1:
                    for i in range(1, len(parts)):
                        unique_folders.add(parts[:i])
            
            # Sort by depth and create in parallel levels
            sorted_folders = sorted(list(unique_folders), key=len)
            depth_map = {}
            for folder_parts in sorted_folders:
                depth = len(folder_parts)
                if depth not in depth_map:
                    depth_map[depth] = []
                depth_map[depth].append(folder_parts)
            
            # Map path parts to IDs for parent lookup
            path_to_id_map = {(): root_folder_id}
            path_to_id_map.update(existing_structure)
            path_map_lock = threading.Lock()

            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS
            pending_folders = set()  # Track folders being created to prevent duplicates
            
            with ThreadPoolExecutor(max_workers=max_workers) as folder_executor:
                for depth in sorted(depth_map.keys()):
                    # Collect folders to create at this depth (under lock)
                    folders_to_create = []
                    with path_map_lock:
                        for folder_parts in depth_map[depth]:
                            # Skip folders that already exist or are being created
                            if folder_parts in path_to_id_map or folder_parts in pending_folders:
                                continue
                            # Get parent ID, skip if parent doesn't exist
                            parent_path = folder_parts[:-1]
                            if parent_path not in path_to_id_map:
                                continue
                            parent_id = path_to_id_map[parent_path]
                            folder_name = folder_parts[-1]
                            folders_to_create.append((folder_parts, parent_id, folder_name))
                            pending_folders.add(folder_parts)
                    
                    # Submit tasks without holding the lock
                    futures_to_parts = {}
                    for folder_parts, parent_id, folder_name in folders_to_create:
                        futures_to_parts[folder_executor.submit(self._worker_create_folder, parent_id, folder_name)] = folder_parts
                    
                    for future in as_completed(futures_to_parts):
                        parts = futures_to_parts[future]
                        try:
                            folder_id = future.result()
                            with path_map_lock:
                                path_to_id_map[parts] = folder_id
                                pending_folders.discard(parts)
                        except Exception as e:
                            with path_map_lock:
                                pending_folders.discard(parts)
                            state.upload.errors.append(f"Failed to create folder {'/'.join(parts)}: {str(e)}")

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
                future_to_file = {executor.submit(self._worker_upload, f, root_folder_id): f for f in files_sorted}

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

    def _list_folder_contents(self, folder_id: str) -> List[Dict]:
        """List immediate contents of a folder (files and subfolders)"""
        service = self.get_service()
        items = []
        page_token = None
        while True:
            query = f"'{folder_id}' in parents and trashed=false"
            results = service.files().list(
                q=query,
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType, size)',
                pageSize=1000,
                pageToken=page_token
            ).execute()
            items.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        return items

    def list_drive_files_recursive(self, service, folder_id: str, path: str = '', max_workers: int = None) -> List[Dict]:
        """List all files using parallel breadth-first traversal"""
        files = []
        # Queue: list of (folder_id, path) to process
        current_level = [(folder_id, path)]
        
        if max_workers is None:
            max_workers = DEFAULT_MAX_WORKERS
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while current_level:
                next_level = []

                # Batch submissions to cap in-flight futures and avoid memory spikes
                batch_size = max_workers * 10
                for i in range(0, len(current_level), batch_size):
                    batch = current_level[i:i + batch_size]
                    future_to_info = {
                        executor.submit(self._list_folder_contents, fid): (fid, folder_path)
                        for fid, folder_path in batch
                    }
                    
                    for future in as_completed(future_to_info):
                        _, folder_path = future_to_info[future]
                        try:
                            items = future.result()
                            for item in items:
                                mime_type = item['mimeType']
                                item_path = os.path.join(folder_path, item['name']) if folder_path else item['name']
                                
                                if mime_type == 'application/vnd.google-apps.folder':
                                    next_level.append((item['id'], item_path))
                                elif mime_type in WORKSPACE_EXPORT_FORMATS:
                                    export_mime, ext = WORKSPACE_EXPORT_FORMATS[mime_type]
                                    files.append({
                                        'id': item['id'],
                                        'name': item['name'],
                                        'path': item_path + ext,
                                        'size': 0,
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
                        except Exception:
                            pass  # Skip folders we can't access
                
                current_level = next_level
        
        return files

    def download_single_file(self, service, file_info: Dict, dest_path: str, progress_state=None) -> Dict:
        """Download a single file from Google Drive with retry logic"""
        file_id = file_info['id']
        rel_path = file_info['path']
        file_size = file_info['size']
        export_mime = file_info.get('export_mime')
        
        # Default to restore state for backward compatibility
        if progress_state is None:
            progress_state = state.restore

        full_path = os.path.join(dest_path, rel_path)

        # Security check: Ensure the file path is within the destination directory
        if not is_safe_path(full_path, dest_path):
            return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Security violation: File path traverses outside destination'}

        parent_dir = os.path.dirname(full_path)
        
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
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
                            # Clean up partial file on cancel
                            try:
                                os.remove(full_path)
                            except OSError:
                                pass
                            return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Cancelled'}
                        while progress_state.is_paused and not progress_state.cancel_requested:
                            time.sleep(0.1)
                        status, done = downloader.next_chunk()

                # Get actual file size after download
                actual_size = os.path.getsize(full_path) if os.path.exists(full_path) else file_size
                return {'success': True, 'file': rel_path, 'size': actual_size, 'error': None}

            except Exception as e:
                last_error = e
                # Clean up partial file before retry
                try:
                    if os.path.exists(full_path):
                        os.remove(full_path)
                except OSError:
                    pass
                
                if attempt < MAX_RETRIES:
                    # Exponential backoff with jitter
                    sleep_time = (INITIAL_BACKOFF * (2 ** attempt)) + random.random()
                    time.sleep(sleep_time)
        
        return {'success': False, 'file': rel_path, 'size': file_size, 'error': str(last_error)}

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

            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS
            
            socketio.emit('restore_status', {'message': 'Scanning Drive folder...'})
            files = self.list_drive_files_recursive(service, folder_id, path='', max_workers=max_workers)

            if not files:
                state.restore.stop()
                socketio.emit('restore_complete', {'message': 'No files to restore'})
                return

            total_bytes = sum(f['size'] for f in files if not f.get('export_mime'))
            
            # Determine destination path
            if contents_only:
                full_dest = dest_path
            else:
                full_dest = os.path.join(dest_path, folder_name)
            
            # Check available disk space (with 10% buffer)
            try:
                os.makedirs(full_dest, exist_ok=True)
                disk_stats = os.statvfs(full_dest)
                available_bytes = disk_stats.f_bavail * disk_stats.f_frsize
                required_bytes = int(total_bytes * 1.1)  # 10% buffer for Workspace file exports
                
                if available_bytes < required_bytes:
                    state.restore.stop()
                    error_msg = f'Insufficient disk space: {available_bytes // (1024*1024)}MB available, {required_bytes // (1024*1024)}MB required'
                    socketio.emit('restore_error', {'message': error_msg})
                    return
            except OSError as e:
                state.restore.errors.append(f"Could not check disk space: {str(e)}")
            
            state.restore.start(len(files), total_bytes)

            socketio.emit('restore_started', {
                'total_files': state.restore.total_files,
                'total_bytes': state.restore.total_bytes
            })

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
                future_to_file = {executor.submit(self._worker_download, f, full_dest, state.restore): f for f in files}

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
            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS

            socketio.emit('transfer_status', {'message': 'Scanning selected items...'})
            
            # Collect all files from selected items
            all_files = []
            scan_errors = []
            for item in items:
                if item.get('type') == 'folder':
                    # Recursively list files in folder
                    folder_files = self.list_drive_files_recursive(service, item['id'], path='', max_workers=max_workers)
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
                        scan_errors.append(f"Failed to get metadata for {item['name']}: {str(e)}")

            # Report scan errors even if no files found
            if scan_errors:
                state.transfer.errors.extend(scan_errors)

            if not all_files:
                state.transfer.stop()
                error_message = 'No files to transfer' + (f'. Errors: {"; ".join(scan_errors)}' if scan_errors else '')
                socketio.emit('transfer_complete', {'message': error_message})
                return

            total_files = len(all_files)
            total_bytes = sum(f.get('size', 0) for f in all_files)
            state.transfer.start(total_files, total_bytes)

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
                future_to_file = {executor.submit(self._worker_download, f, dest_path, state.transfer): f for f in all_files}

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
