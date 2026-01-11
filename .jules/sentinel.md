## 2024-03-24 - Path Traversal Vulnerability in Flask File Browser
**Vulnerability:** Arbitrary File Read / Path Traversal
**Learning:** The application allowed users to browse and scan any directory on the server filesystem by passing an absolute path to the `/api/browse` and `/api/scan` endpoints. This occurred because the user-supplied `path` parameter was used directly in `os.listdir` and `os.walk` without validating it against a permitted root directory.
**Prevention:** Always validate user-supplied file paths against a whitelist of allowed root directories. Use `os.path.commonpath([safe_root, target_path]) == safe_root` to verify that the resolved target path is contained within the safe root, effectively blocking `..` traversal and absolute paths pointing elsewhere.
