import os
import threading
import time
import random
import mimetypes
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials

from app.state import state, ProgressState
from app.services.files import get_files_to_upload
from app.config import UploadConfig, load_upload_config, AppConfig

# Constants
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB chunks (reduced to be more granular for progress)
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

    def _worker_create_folder(self, parent_id, folder_name):
        """Worker task for creating folders"""
        service = self.get_service()
        return self.get_or_create_folder(service, folder_name, parent_id)

    def _worker_upload(self, file_info, root_folder_id, progress_state):
        """Worker task for uploading files"""
        service = self.get_service()
        return self.upload_single_file(service, file_info, root_folder_id, progress_state)

    def _worker_download(self, file_info, dest_path, progress_state):
        """Worker task for downloading files"""
        # Ensure we don't start download if cancelled
        if progress_state.cancel_requested:
            return {'success': False, 'file': file_info['path'], 'size': file_info['size'], 'error': 'Cancelled'}

        service = self.get_service()
        return self.download_single_file(service, file_info, dest_path, progress_state)

    def upload_single_file(self, service, file_info: Dict, root_folder_id: str, progress_state: ProgressState) -> Dict:
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
                    service.files().update(fileId=file_id, body=file_metadata, fields='id').execute()
                else:
                    service.files().create(body=file_metadata, fields='id').execute()
            elif file_size < RESUMABLE_THRESHOLD:
                # Simple upload with retry logic
                media = MediaFileUpload(file_path, mimetype=mime_type, resumable=False)
                retries = 0
                while True:
                    try:
                        if update_mode:
                            service.files().update(fileId=file_id, media_body=media, fields='id').execute()
                        else:
                            service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                        break
                    except Exception as e:
                        retries += 1
                        if retries > MAX_RETRIES:
                            raise e
                        sleep_time = (INITIAL_BACKOFF * (2 ** (retries - 1))) + random.random()
                        time.sleep(sleep_time)
                # For simple upload, update progress all at once
                progress_state.update_progress(rel_path, bytes_processed=file_size)
            else:
                # Resumable upload for larger files
                media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True, chunksize=CHUNK_SIZE)
                if update_mode:
                    request = service.files().update(fileId=file_id, media_body=media, fields='id')
                else:
                    request = service.files().create(body=file_metadata, media_body=media, fields='id')

                response = None
                retries = 0
                last_progress = 0

                while response is None:
                    if progress_state.cancel_requested:
                        return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Cancelled'}
                    while progress_state.is_paused and not progress_state.cancel_requested:
                        time.sleep(0.1)
                    
                    try:
                        status, response = request.next_chunk()
                        retries = 0  # Reset retries on success

                        if status:
                            current_progress = int(status.resumable_progress)
                            bytes_delta = current_progress - last_progress
                            if bytes_delta > 0:
                                progress_state.update_progress(rel_path, bytes_processed=bytes_delta)
                                last_progress = current_progress
                    except Exception as e:
                        retries += 1
                        if retries > MAX_RETRIES:
                            raise e
                        sleep_time = (INITIAL_BACKOFF * (2 ** (retries - 1))) + random.random()
                        time.sleep(sleep_time)
                        continue

                # Ensure we account for any remaining bytes (or if logic skipped exactly hitting 100%)
                if last_progress < file_size:
                    progress_state.update_progress(rel_path, bytes_processed=file_size - last_progress)

            return {'success': True, 'file': rel_path, 'size': file_size, 'error': None}

        except Exception as e:
            return {'success': False, 'file': rel_path, 'size': file_size, 'error': str(e)}

    def download_single_file(self, service, file_info: Dict, dest_path: str, progress_state: ProgressState) -> Dict:
        """Download a single file from Google Drive with retry logic"""
        file_id = file_info['id']
        rel_path = file_info['path']
        file_size = file_info['size']
        export_mime = file_info.get('export_mime')

        full_path = os.path.join(dest_path, rel_path)
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
                    downloader = MediaIoBaseDownload(f, request, chunksize=CHUNK_SIZE)
                    done = False
                    last_progress = 0

                    while not done:
                        if progress_state.cancel_requested:
                            # Clean up partial file on cancel
                            try:
                                f.close()
                                os.remove(full_path)
                            except OSError:
                                pass
                            return {'success': False, 'file': rel_path, 'size': file_size, 'error': 'Cancelled'}
                        while progress_state.is_paused and not progress_state.cancel_requested:
                            time.sleep(0.1)

                        status, done = downloader.next_chunk()

                        if status and not export_mime: # We don't know size of export, so skip byte progress
                             current_progress = int(status.resumable_progress)
                             bytes_delta = current_progress - last_progress
                             if bytes_delta > 0:
                                 progress_state.update_progress(rel_path, bytes_processed=bytes_delta)
                                 last_progress = current_progress

                # Get actual file size after download
                actual_size = os.path.getsize(full_path) if os.path.exists(full_path) else file_size

                # If we missed any bytes (or it was an export where we couldn't track), update now
                # Note: for export, file_size was 0 in input, so we use actual_size
                bytes_to_add = 0
                if export_mime:
                    bytes_to_add = actual_size # We didn't track anything during download
                elif last_progress < actual_size:
                    bytes_to_add = actual_size - last_progress

                if bytes_to_add > 0:
                    progress_state.update_progress(rel_path, bytes_processed=bytes_to_add)

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

    def _execute_drive_process(self,
                             files: List[Dict],
                             worker_func: Any,
                             state_obj: ProgressState,
                             socketio: Any,
                             event_prefix: str,
                             max_workers: int,
                             process_args: Tuple = (),
                             total_bytes: int = None):
        """Generic execution loop for drive processes"""

        if total_bytes is None:
            total_bytes = sum(f.get('size', 0) for f in files)

        state_obj.start(len(files), total_bytes)
        
        socketio.emit(f'{event_prefix}_started', {
            'total_files': state_obj.total_files,
            'total_bytes': state_obj.total_bytes
        })

        last_emit_time = 0
        def emit_progress(force=False):
            nonlocal last_emit_time
            now = time.time()
            if force or now - last_emit_time > 0.5:
                socketio.emit(f'{event_prefix}_progress', state_obj.to_dict())
                last_emit_time = now

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_file = {executor.submit(worker_func, f, *process_args, state_obj): f for f in files}

            for future in as_completed(future_to_file):
                if state_obj.cancel_requested:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                result = future.result()
                
                # Check for success
                if result['success']:
                    state_obj.update_progress(result['file'], file_completed=True)
                else:
                    state_obj.update_progress(result['file'], file_completed=True, error=result['error'])
                    if result['error'] != 'Cancelled':
                        socketio.emit(f'{event_prefix}_error', {'message': f"Error {result['file']}: {result['error']}", 'file': result['file']})

                emit_progress()

        emit_progress(force=True)
        
        return state_obj.to_dict()

    def run_upload_process(self, config: UploadConfig, socketio):
        """Background process for uploading"""
        try:
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

            # Pre-create folder structure to avoid bottlenecks in parallel workers
            socketio.emit('upload_status', {'message': 'Preparing folder structure...'})
            root_folder_id = self.get_or_create_folder(service, config.drive_folder_name)
            
            # Prefetch existing structure
            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS
            existing_structure = self.list_folders_recursive(service, root_folder_id, max_workers=max_workers)
            with self.folder_cache_lock:
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
            
            # Create missing folders
            sorted_folders = sorted(list(unique_folders), key=len)
            depth_map = {}
            for folder_parts in sorted_folders:
                depth = len(folder_parts)
                if depth not in depth_map:
                    depth_map[depth] = []
                depth_map[depth].append(folder_parts)
            
            path_to_id_map = {(): root_folder_id}
            path_to_id_map.update(existing_structure)
            path_map_lock = threading.Lock()
            pending_folders = set()
            
            with ThreadPoolExecutor(max_workers=max_workers) as folder_executor:
                for depth in sorted(depth_map.keys()):
                    folders_to_create = []
                    with path_map_lock:
                        for folder_parts in depth_map[depth]:
                            if folder_parts in path_to_id_map or folder_parts in pending_folders:
                                continue
                            parent_path = folder_parts[:-1]
                            if parent_path not in path_to_id_map:
                                continue
                            parent_id = path_to_id_map[parent_path]
                            folder_name = folder_parts[-1]
                            folders_to_create.append((folder_parts, parent_id, folder_name))
                            pending_folders.add(folder_parts)
                    
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

            # Prioritize small files to show progress quickly? Or large ones?
            # Actually mixing is usually fine, but sorting by size helps pack chunks.
            files_sorted = sorted(files, key=lambda x: x['size'])

            # Execute Upload
            result_state = self._execute_drive_process(
                files=files_sorted,
                worker_func=self._worker_upload,
                state_obj=state.upload,
                socketio=socketio,
                event_prefix='upload',
                max_workers=max_workers,
                process_args=(root_folder_id,)
            )

            elapsed = time.time() - (result_state['start_time'] or time.time())
            socketio.emit('upload_complete', {
                'processed_files': result_state['processed_files'],
                'uploaded_bytes': result_state['uploaded_bytes'],
                'elapsed_seconds': elapsed,
                'errors': result_state['errors'],
                'cancelled': result_state['cancel_requested']
            })

        except Exception as e:
            socketio.emit('upload_error', {'message': f'Upload failed: {str(e)}'})
        finally:
            state.upload.stop()

    def run_restore_process(self, folder_id: str, folder_name: str, dest_path: str, contents_only: bool, socketio):
        try:
            if not self.credentials_provider.is_authenticated():
                state.restore.errors.append("Not authenticated")
                state.restore.stop()
                socketio.emit('restore_error', {'message': 'Not authenticated'})
                return

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
            
            if contents_only:
                full_dest = dest_path
            else:
                full_dest = os.path.join(dest_path, folder_name)
            
            try:
                os.makedirs(full_dest, exist_ok=True)
                disk_stats = os.statvfs(full_dest)
                available_bytes = disk_stats.f_bavail * disk_stats.f_frsize
                required_bytes = int(total_bytes * 1.1)
                
                if available_bytes < required_bytes:
                    state.restore.stop()
                    error_msg = f'Insufficient disk space: {available_bytes // (1024*1024)}MB available, {required_bytes // (1024*1024)}MB required'
                    socketio.emit('restore_error', {'message': error_msg})
                    return
            except OSError as e:
                state.restore.errors.append(f"Could not check disk space: {str(e)}")
            
            socketio.emit('restore_status', {'message': f'Downloading {len(files)} files...'})

            # Execute Restore
            result_state = self._execute_drive_process(
                files=files,
                worker_func=self._worker_download,
                state_obj=state.restore,
                socketio=socketio,
                event_prefix='restore',
                max_workers=max_workers,
                process_args=(full_dest,),
                total_bytes=total_bytes
            )

            elapsed = time.time() - (result_state['start_time'] or time.time())
            socketio.emit('restore_complete', {
                'processed_files': result_state['processed_files'],
                'downloaded_bytes': result_state['downloaded_bytes'],
                'elapsed_seconds': elapsed,
                'errors': result_state['errors'],
                'cancelled': result_state['cancel_requested'],
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

            app_config = AppConfig()
            config = load_upload_config(app_config.CONFIG_FILE)
            service = self.get_service()
            max_workers = config.max_workers if hasattr(config, 'max_workers') else DEFAULT_MAX_WORKERS

            socketio.emit('transfer_status', {'message': 'Scanning selected items...'})
            
            all_files = []
            scan_errors = []
            for item in items:
                if item.get('type') == 'folder':
                    folder_files = self.list_drive_files_recursive(service, item['id'], path='', max_workers=max_workers)
                    all_files.extend(folder_files)
                else:
                    try:
                        file_meta = service.files().get(
                            fileId=item['id'],
                            fields='id,name,mimeType,size'
                        ).execute()
                        
                        file_name = file_meta['name']
                        mime_type = file_meta['mimeType']
                        export_mime = None
                        
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

            if scan_errors:
                state.transfer.errors.extend(scan_errors)

            if not all_files:
                state.transfer.stop()
                error_message = 'No files to transfer' + (f'. Errors: {"; ".join(scan_errors)}' if scan_errors else '')
                socketio.emit('transfer_complete', {'message': error_message})
                return

            socketio.emit('transfer_status', {'message': f'Transferring {len(all_files)} files...'})

            # Execute Transfer
            result_state = self._execute_drive_process(
                files=all_files,
                worker_func=self._worker_download,
                state_obj=state.transfer,
                socketio=socketio,
                event_prefix='transfer',
                max_workers=max_workers,
                process_args=(dest_path,)
            )

            elapsed = time.time() - (result_state['start_time'] or time.time())
            socketio.emit('transfer_complete', {
                'processed_files': result_state['processed_files'],
                'downloaded_bytes': result_state['downloaded_bytes'],
                'elapsed_seconds': elapsed,
                'errors': result_state['errors'],
                'cancelled': result_state['cancel_requested'],
                'dest_path': dest_path
            })

        except Exception as e:
            socketio.emit('transfer_error', {'message': f'Transfer failed: {str(e)}'})
        finally:
            state.transfer.stop()

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
    def list_folders_recursive(self, service, folder_id: str, current_path_parts: tuple = (), max_workers: int = None) -> Dict[tuple, str]:
        """List all folders using parallel breadth-first traversal"""
        found_folders = {}
        current_level = [(folder_id, current_path_parts)]

        if max_workers is None:
            max_workers = DEFAULT_MAX_WORKERS

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while current_level:
                next_level = []
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
                            pass

                current_level = next_level

        return found_folders

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
        current_level = [(folder_id, path)]

        if max_workers is None:
            max_workers = DEFAULT_MAX_WORKERS

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while current_level:
                next_level = []
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
                            pass

                current_level = next_level

        return files
