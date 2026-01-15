## 2026-01-15 - Unchecked Configuration Path Traversal
**Vulnerability:** The `/api/config` endpoint allowed saving an arbitrary `source_path` (e.g., `/`) without validation. The `/api/upload/start` endpoint then used this path to read files from the filesystem, allowing full filesystem access (Path Traversal).
**Learning:** Saving configuration that controls file access is a critical security boundary. Even if the immediate saving seems harmless, the *use* of that configuration later is where the vulnerability manifests. Validation must happen at the input gate (API) and ideally also at the usage point (Service).
**Prevention:**
1. Validate all file paths against a safe root (e.g., `/workspace`) using `is_safe_path` before accepting them in API endpoints.
2. Validate paths again when loading them from configuration files before performing operations.
3. Use defense-in-depth: API validation + Service validation.
