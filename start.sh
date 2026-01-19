#!/bin/bash

# Start RunPod2Drive in the background
echo "Starting RunPod2Drive..."
cd /workspace/RunPod2Drive
# We do NOT pass --port here so run.py can use the env var or default
nohup python run.py > /workspace/runpod2drive.log 2>&1 &

# Start ReForge (Stable Diffusion WebUI)
echo "Starting ReForge on port 7860..."
cd /workspace/stable-diffusion-webui-reForge

# We need to run as a user usually, but root is fine in this container context.
# We add --listen to ensure it's accessible externally.
# We also add --api if users want to use it via API.
./webui.sh --listen --port 7860 --api
