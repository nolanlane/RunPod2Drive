## 2024-05-22 - Secure Random Default for Secret Key
**Vulnerability:** Hardcoded `SECRET_KEY` in `AppConfig` default value.
**Learning:** `pydantic`'s `default_factory` is powerful for generating secure defaults that persist (via file storage) without requiring user intervention, but must handle filesystem permissions (read-only) gracefully.
**Prevention:** Avoid hardcoded secrets even as defaults. Use `secrets.token_hex()` and file persistence pattern.
