import threading
import time
from typing import List, Dict, Any, Optional

class ProgressState:
    def __init__(self):
        self._lock = threading.Lock()
        self.is_running: bool = False
        self.is_paused: bool = False
        self.current_file: str = ''
        self.total_files: int = 0
        self.processed_files: int = 0
        self.total_bytes: int = 0
        self.processed_bytes: int = 0  # uploaded or downloaded bytes
        self.errors: List[str] = []
        self.start_time: Optional[float] = None
        self.last_update_time: Optional[float] = None
        self.cancel_requested: bool = False

    def start(self, total_files: int, total_bytes: int):
        with self._lock:
            self.is_running = True
            self.is_paused = False
            self.current_file = ''
            self.total_files = total_files
            self.total_bytes = total_bytes
            self.processed_files = 0
            self.processed_bytes = 0
            self.errors = []
            self.start_time = time.time()
            self.last_update_time = self.start_time
            self.cancel_requested = False

    def update_progress(self, current_file: str, bytes_processed: int = 0, file_completed: bool = False, error: str = None):
        with self._lock:
            self.current_file = current_file
            if bytes_processed > 0:
                self.processed_bytes += bytes_processed
                self.last_update_time = time.time()

            if file_completed:
                self.processed_files += 1

            if error:
                self.errors.append(error)
    
    def update_total(self, total_files: int, total_bytes: int):
        with self._lock:
            self.total_files = total_files
            self.total_bytes = total_bytes

    def set_paused(self, paused: bool):
        with self._lock:
            self.is_paused = paused

    def request_cancel(self):
        with self._lock:
            self.cancel_requested = True

    def stop(self):
        with self._lock:
            self.is_running = False
            self.cancel_requested = False

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'is_running': self.is_running,
                'is_paused': self.is_paused,
                'current_file': self.current_file,
                'total_files': self.total_files,
                'processed_files': self.processed_files,
                'total_bytes': self.total_bytes,
                'uploaded_bytes': self.processed_bytes, # API compat
                'downloaded_bytes': self.processed_bytes, # API compat
                'errors': self.errors[:],
                'start_time': self.start_time,
                'last_update_time': self.last_update_time,
                'cancel_requested': self.cancel_requested
            }

class StateManager:
    def __init__(self):
        self.upload = ProgressState()
        self.restore = ProgressState()
        self.transfer = ProgressState()

# Global state instance
state = StateManager()
