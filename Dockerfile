# Use a modern PyTorch base image compatible with ReForge (PyTorch 2.4 + CUDA 12.4)
FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PATH="/workspace/venv/bin:$PATH"

# Install system dependencies
# build-essential added for compiling extensions if needed
RUN apt-get update && apt-get install -y \
    git \
    wget \
    curl \
    libgl1 \
    libglib2.0-0 \
    google-perftools \
    bc \
    python3-venv \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set up workspace
WORKDIR /workspace

# Install ReForge
RUN git clone https://github.com/Panchovix/stable-diffusion-webui-reForge.git stable-diffusion-webui-reForge

# Install ReForge dependencies
# Using requirements_versions.txt as it pins compatible versions
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
