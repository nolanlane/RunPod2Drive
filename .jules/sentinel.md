# Sentinel Journal

## 2024-05-22 - [CRITICAL] Hardcoded SECRET_KEY
**Vulnerability:** The application was using a hardcoded default `SECRET_KEY` ('dev-secret-key-change-in-prod') in `app/config.py`.
**Learning:** Default values in Pydantic models can accidentally become production values if environment variables are missing.
**Prevention:** Use `default_factory` to generate secure random keys if no environment variable is provided, rather than falling back to a known static string.
