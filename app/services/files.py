import os
import fnmatch
from pathlib import Path
from datetime import datetime
from typing import List, Dict

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

def is_safe_path(path: str, base_directory: str = '/workspace') -> bool:
    """Check if the path is within the allowed base directory"""
    try:
        resolved_path = os.path.abspath(path)
        resolved_base = os.path.abspath(base_directory)
        return os.path.commonpath([resolved_path, resolved_base]) == resolved_base
    except Exception:
        return False

def get_files_to_upload(source_path: str, exclusions: List[str],
                        include_hidden: bool, max_size_mb: int) -> List[Dict]:
    """Get list of files to upload with their info"""
    files = []
    source_path = os.path.abspath(source_path)
    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else float('inf')

    if not os.path.exists(source_path):
        return []

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

def browse_directory(path: str) -> Dict:
    """Browse filesystem directories"""
    if not os.path.exists(path):
        raise FileNotFoundError('Path does not exist')

    if not os.path.isdir(path):
        raise NotADirectoryError('Not a directory')

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
        raise PermissionError('Permission denied')

    parent = os.path.dirname(path) if path != '/' else None

    return {
        'current': path,
        'parent': parent,
        'items': items
    }
