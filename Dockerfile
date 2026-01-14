# Use a modern PyTorch base image compatible with ReForge
# ReForge often works with PyTorch 2.1+, so we use 2.1.2 which is very stable for SD WebUI
FROM pytorch/pytorch:2.1.2-cuda12.1-cudnn8-runtime

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PATH="/workspace/venv/bin:$PATH"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    wget \
    curl \
    libgl1 \
    libglib2.0-0 \
    google-perftools \
    bc \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Set up workspace
WORKDIR /workspace

# Install ReForge
RUN git clone https://github.com/Panchovix/stable-diffusion-webui-reForge.git stable-diffusion-webui-reForge

# Install ReForge dependencies
# We use the requirements_versions.txt we inspected earlier
WORKDIR /workspace/stable-diffusion-webui-reForge
RUN pip install -r requirements_versions.txt

# Install RunPod2Drive
WORKDIR /workspace
COPY . /workspace/RunPod2Drive

# Install RunPod2Drive dependencies
WORKDIR /workspace/RunPod2Drive
RUN pip install -r requirements.txt

# Create start script
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Expose ports
# 8080 for RunPod2Drive
# 7860 for ReForge (default)
EXPOSE 8080 7860

# Set entrypoint
CMD ["/start.sh"]
