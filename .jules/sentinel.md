## 2024-05-22 - Hardcoded Secret Key in Configuration
**Vulnerability:** The application used a hardcoded string 'dev-secret-key-change-in-prod' as the default `SECRET_KEY` in `app/config.py`.
**Learning:** Even with Pydantic settings, using a static string in `default=` embeds that secret in the code.
**Prevention:** Use `default_factory` with `secrets.token_hex` (or similar) to generate secure defaults when environment variables are missing.
