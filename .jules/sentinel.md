## 2025-02-20 - Hardcoded Secrets in Default Configuration
**Vulnerability:** The `SECRET_KEY` in `AppConfig` defaulted to a hardcoded string `'dev-secret-key-change-in-prod'`. While intended for development, this could leave production instances vulnerable to session hijacking if the environment variable is not set.
**Learning:** Default values in configuration classes (like Pydantic Settings) should be secure by default, especially for self-hosted applications where users might skip detailed configuration.
**Prevention:** Use `default_factory` to generate secure, persistent secrets if they are not explicitly provided.
