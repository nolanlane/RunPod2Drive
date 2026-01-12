## 2024-05-24 - Hardcoded Secret Key Default
**Vulnerability:** The `SECRET_KEY` configuration had a hardcoded default value ('dev-secret-key-change-in-prod').
**Learning:** Hardcoded defaults for sensitive values in open source projects often end up in production because users forget to override them.
**Prevention:** Use `default_factory` with `secrets.token_hex()` to generate a secure random value if the environment variable is missing. This ensures security by default ("Fail Secure"), even if it means session invalidation on restart (which is better than a compromised session).
