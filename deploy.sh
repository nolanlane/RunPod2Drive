#!/bin/bash
# Deploy RunPod2Drive to a RunPod instance

set -e

if [ -z "$1" ]; then
    echo "Usage: ./deploy.sh <runpod-ssh-address>"
    echo "Example: ./deploy.sh root@123.45.67.89"
    echo "         ./deploy.sh ssh://root@123.45.67.89:22001"
    exit 1
fi

REMOTE="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "📦 Packaging RunPod2Drive..."
cd "$SCRIPT_DIR"

# Create a tarball excluding unnecessary files
tar -czf /tmp/runpod2drive.tar.gz \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='token.json' \
    --exclude='server.log' \
    --exclude='config.json' \
    runpod2drive.py \
    requirements.txt \
    README.md \
    credentials.json.example \
    templates/ \
    static/ 2>/dev/null || true

echo "📤 Uploading to RunPod..."
scp /tmp/runpod2drive.tar.gz "$REMOTE":/workspace/

echo "📂 Extracting on RunPod..."
ssh "$REMOTE" "cd /workspace && mkdir -p RunPod2Drive && tar -xzf runpod2drive.tar.gz -C RunPod2Drive && rm runpod2drive.tar.gz"

echo "📦 Installing dependencies..."
ssh "$REMOTE" "cd /workspace/RunPod2Drive && pip install -r requirements.txt"

echo ""
echo "✅ Deployed successfully!"
echo ""
echo "Next steps on your RunPod:"
echo "  1. cd /workspace/RunPod2Drive"
echo "  2. Copy your credentials.json file there"
echo "  3. python runpod2drive.py --port 7860"
echo ""
echo "Then access via your RunPod's exposed port or proxy URL"
