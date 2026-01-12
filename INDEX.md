# RunPod2Drive Project Index

## Overview
RunPod2Drive is a web-based utility designed to streamline the process of backing up files from a RunPod instance to Google Drive. It features a modern web UI, OAuth authentication, and real-time progress tracking.

## Project Structure

### Root Directory
- `@/home/nolan/RunPod2Drive/run.py:1-41`: Entry point for the application. Handles command-line arguments and starts the Flask-SocketIO server.
- `@/home/nolan/RunPod2Drive/wsgi.py:1`: WSGI entry point for production deployments.
- `@/home/nolan/RunPod2Drive/requirements.txt:1`: List of Python dependencies (Flask, SocketIO, Google API clients, etc.).
- `@/home/nolan/RunPod2Drive/README.md:1-133`: Project documentation, setup instructions, and feature overview.
- `@/home/nolan/RunPod2Drive/deploy.sh:1`: Script for deployment automation.
- `@/home/nolan/RunPod2Drive/agent.md:1`: Development notes or agent-specific documentation.
- `@/home/nolan/RunPod2Drive/credentials.json.example:1`: Template for Google OAuth credentials.

### Application Core (`/app`)
- `@/home/nolan/RunPod2Drive/app/__init__.py:1`: Flask application factory and SocketIO initialization.
- `@/home/nolan/RunPod2Drive/app/config.py:1-127`: Configuration management, Pydantic models, and predefined upload presets (Default, ML Training, Full Backup, etc.).
- `@/home/nolan/RunPod2Drive/app/state.py:1`: Global application state management, including upload progress and status.
- `@/home/nolan/RunPod2Drive/app/views.py:1`: Main route for serving the web interface.

### API & Routes (`/app/api`)
- `@/home/nolan/RunPod2Drive/app/api/routes.py:1-127`: API endpoints for authentication, file browsing, and controlling upload tasks.

### Services (`/app/services`)
- `@/home/nolan/RunPod2Drive/app/services/auth.py:1`: Google OAuth2 flow implementation and token management.
- `@/home/nolan/RunPod2Drive/app/services/drive.py:1`: Google Drive API integration for folder creation and file uploads.
- `@/home/nolan/RunPod2Drive/app/services/files.py:1`: Local file system utilities for scanning and filtering files based on patterns.
- `@/home/nolan/RunPod2Drive/app/services/instances.py:1`: (Metadata suggests internal service for instance management).

### Frontend (`/app/templates`)
- `@/home/nolan/RunPod2Drive/app/templates/index.html:1`: Single-page application (SPA) template containing the UI and client-side logic.

## Key Features & Logic
- **Authentication**: Uses Google OAuth 2.0. Credentials are stored in `token.json` after the initial flow.
- **Upload Engine**: Supports multi-threaded/asynchronous uploads with progress updates sent via SocketIO.
- **Exclusion System**: Flexible glob-based filtering to skip logs, caches, and large binaries.
- **Presets**: Pre-configured exclusion lists for common development environments (Python, ML, Node.js).
