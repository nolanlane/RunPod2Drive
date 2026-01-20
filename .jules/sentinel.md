## 2024-05-23 - Hardcoded Secret Key in Configuration
**Vulnerability:** The application was using a hardcoded `SECRET_KEY` ("dev-secret-key-change-in-prod") in `app/config.py` as a default. This allows attackers to forge session cookies and potentially bypass CSRF protections (if implemented) or hijack user sessions if the app is exposed.
**Learning:** Default values in Pydantic models for sensitive secrets can be dangerous if they are valid strings that "work" but provide no security. It's better to fail or generate a secure default dynamically.
**Prevention:** Use `default_factory` to generate secure random keys if not provided via environment variables, and persist them locally for consistency across restarts, while ensuring they are git-ignored.
