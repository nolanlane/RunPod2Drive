## 2025-01-26 - Hardcoded Secret Key in Configuration
**Vulnerability:** The `SECRET_KEY` was hardcoded to a default string in `app/config.py`, which would be used in production if not explicitly overridden by an environment variable. This compromised session security.
**Learning:** `BaseSettings` defaults are convenient but dangerous for secrets. Ephemeral environments like RunPod often lack persistent configuration management, leading users to rely on defaults.
**Prevention:** Use `default_factory` to generate secure random values at runtime when secrets are missing, and optionally persist them to disk for continuity across restarts.
