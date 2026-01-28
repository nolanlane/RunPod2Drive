## 2024-05-22 - Hardcoded Secret Key
**Vulnerability:** The application used a hardcoded default `SECRET_KEY` 'dev-secret-key-change-in-prod' in `app/config.py`.
**Learning:** Default values in configuration classes can be dangerous if not overridden in production. Relying on users to change defaults is a common failure mode.
**Prevention:** Use a factory to generate a secure random key if one is not provided, and persist it locally for development convenience, but never commit it.
