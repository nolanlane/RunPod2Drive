## 2025-02-18 - [CRITICAL] Path Traversal in File Restore
**Vulnerability:** The `download_single_file` function blindly joined `dest_path` with `rel_path` from Google Drive, allowing arbitrary file overwrite via path traversal (e.g., `../../etc/passwd`).
**Learning:** Never trust file paths from external sources, even "trusted" ones like the user's own Google Drive. `os.path.join` resolves `..` components, which can allow escaping the intended directory.
**Prevention:** Always validate constructed file paths against a safe base directory using a robust check like `is_safe_path` (checking `os.path.commonpath`) before performing any file operations.
