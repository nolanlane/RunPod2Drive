## 2024-05-22 - Hardcoded Pydantic Default Secrets
**Vulnerability:** Hardcoded `SECRET_KEY` in `pydantic-settings` `Field(default='...')`.
**Learning:** Developers often provide insecure defaults in `BaseSettings` classes for convenience in development, assuming they will be overridden by environment variables in production. However, without explicit checks or warnings, these defaults can silently persist in production deployments.
**Prevention:** Use `default_factory` with a function that generates a secure random value (and optionally persists it) or raise an error if the secret is not provided in the environment.
