# Sentinel Journal

## 2024-03-22 - Persistent Secret Key Generation
**Vulnerability:** The application was using a hardcoded `SECRET_KEY` ('dev-secret-key-change-in-prod'), which would allow session forgery if the user didn't manually set an environment variable.
**Learning:** `pydantic-settings` provides a `default_factory` mechanism that is perfect for generating dynamic defaults (like a random key) only when the environment variable is missing. Persisting this key to a local file ensures session continuity across restarts without requiring user configuration.
**Prevention:** Always use `default_factory` with a generation+persistence logic for `SECRET_KEY` in `pydantic-settings` classes to ensure secure defaults out-of-the-box.
