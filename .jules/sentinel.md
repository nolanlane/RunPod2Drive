## 2026-01-17 - [CRITICAL] Path Traversal in Configuration and Upload
**Vulnerability:** The `/api/config` endpoint allowed updating the `source_path` without validation, and `/api/upload/start` utilized this path without checking if it was within the allowed directory (`/workspace`).
**Learning:** Configuration endpoints that modify file system paths must enforce the same security boundaries as file operation endpoints. Assuming configuration data is safe because it's "internal" state is a fallacy when that state is mutable via API.
**Prevention:** Apply `is_safe_path` validation both when saving configuration (to fail early) and when using configuration for critical operations (defense in depth).
