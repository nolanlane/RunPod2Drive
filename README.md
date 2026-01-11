# RunPod2Drive

A web-based tool for uploading files from a RunPod instance to Google Drive with configurable exclusions, presets, and real-time progress tracking.

## Features

- 🔐 **Google OAuth Authentication** - Secure authorization flow
- 📁 **Directory Browser** - Visual file system navigation
- ⚙️ **Configurable Exclusions** - Pattern-based file filtering
- 🎯 **Presets** - Quick configuration templates for different use cases
- 📊 **Real-time Progress** - Live upload status with speed and ETA
- ⏸️ **Pause/Resume/Cancel** - Full upload control
- 🎨 **Modern UI** - Clean, responsive interface

## Prerequisites

### 1. Google Cloud Project Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the **Google Drive API**:
   - Go to "APIs & Services" → "Library"
   - Search for "Google Drive API" and enable it
4. Create OAuth credentials:
   - Go to "APIs & Services" → "Credentials"
   - Click "Create Credentials" → "OAuth client ID"
   - Choose "Web application"
   - Add authorized redirect URI: `http://localhost:7860/api/auth/callback`
     - Replace `localhost:7860` with your RunPod's public URL and port if accessing remotely
   - Download the credentials JSON file

### 2. Place credentials.json

Copy the downloaded credentials file to the same directory as `runpod2drive.py` and rename it to `credentials.json`.

## Installation

```bash
# Clone or copy files to your RunPod instance
cd /workspace  # or your preferred directory

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
# Run with default port (7860)
python runpod2drive.py

# Run with custom port
python runpod2drive.py --port 8080

# Run with debug mode
python runpod2drive.py --debug
```

### Command Line Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `-p, --port` | 7860 | Port to run the web UI |
| `--host` | 0.0.0.0 | Host to bind to |
| `--debug` | False | Enable debug mode |

## Presets

| Preset | Description |
|--------|-------------|
| **Default** | Standard backup excluding common unnecessary files |
| **ML Training** | Optimized for ML projects - keeps models and checkpoints |
| **Full Backup** | Backup everything including hidden files |
| **Code Only** | Only source code files, excludes binaries and media |
| **Minimal** | Essential files only, excludes large binaries |

## Configuration Options

- **Source Directory**: Path to upload from (default: `/workspace`)
- **Drive Folder Name**: Destination folder in Google Drive
- **Exclusion Patterns**: Glob patterns for files/folders to skip
- **Include Hidden Files**: Whether to include dotfiles
- **Max File Size**: Skip files larger than this (0 = no limit)

## Exclusion Patterns

Use glob patterns to exclude files:
- `*.log` - All log files
- `__pycache__` - Python cache directories
- `node_modules` - Node.js dependencies
- `.git` - Git repositories
- `*.pt` - PyTorch model files

## RunPod-Specific Notes

### Accessing the UI

1. **Using RunPod's HTTP Port Forwarding**:
   - Expose port 7860 in your RunPod pod settings
   - Access via the provided URL

2. **Using SSH Tunneling**:
   ```bash
   ssh -L 7860:localhost:7860 root@your-runpod-ip
   ```
   Then open `http://localhost:7860` in your browser

### OAuth Redirect URI

When running on RunPod, update your Google Cloud OAuth credentials to include the appropriate redirect URI:
- For HTTP port forwarding: `https://your-pod-id.proxy.runpod.net/api/auth/callback`
- For SSH tunnel: `http://localhost:7860/api/auth/callback`

## Troubleshooting

### "credentials.json not found"
Ensure the credentials file is in the same directory as the script and named exactly `credentials.json`.

### "Redirect URI mismatch"
Update the authorized redirect URIs in your Google Cloud Console to match your actual URL.

### "Access blocked: This app's request is invalid"
Ensure you've enabled the Google Drive API in your project and the OAuth consent screen is configured.

### Upload Errors
- Check file permissions
- Ensure adequate disk space
- Verify Google Drive storage quota

## License

MIT License
