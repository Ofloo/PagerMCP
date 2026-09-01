#!/usr/bin/env bash
# BSD Zero Clause License
# Copyright (c) 2026 Wouter Snels
set -euo pipefail

# Configuration
IMAGE_NAME="ofloo/pagermcp"
DEFAULT_TAG="latest"
PLATFORMS="linux/amd64,linux/arm64"
BUILDER_NAME="multiarch-builder"

# Parse command line arguments
TAG="${1:-$DEFAULT_TAG}"
BUILD_ARGS=""
BUILD_NUMBER_FILE=".build_number"
BUILD_NUMBER=0
if [[ -f "$BUILD_NUMBER_FILE" ]]; then
    BUILD_NUMBER=$(<"$BUILD_NUMBER_FILE")
fi
BUILD_NUMBER=$((BUILD_NUMBER + 1))
printf '%s\n' "$BUILD_NUMBER" > "$BUILD_NUMBER_FILE"
BUILD_ARGS="--build-arg BUILD_NUMBER=$BUILD_NUMBER"

# Check if we should build for Hailo (ARM64 only)
if [[ "$TAG" == *"-hailo"* ]]; then
    echo "🔧 Hailo build detected - limiting to ARM64"
    PLATFORMS="linux/arm64"
    BUILD_ARGS+=" --build-arg HAILO_BUILD=true"
fi

# Log configuration
echo "🏗️  Building image: ${IMAGE_NAME}:${TAG}"
echo "📦 Platforms: ${PLATFORMS}"
echo "🚀 Build args: ${BUILD_ARGS:-None}"

# Create multiarch builder if it doesn't exist
if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    echo "🔧 Creating new builder: $BUILDER_NAME"
    docker buildx create --name "$BUILDER_NAME" --driver docker-container --use
    docker buildx inspect --bootstrap
fi

# Ensure QEMU is installed for cross-arch emulation
echo "🔄 Setting up QEMU"
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

# Build and push
echo "🚀 Starting build..."
docker buildx build \
    --platform "$PLATFORMS" \
    -t "${IMAGE_NAME}:${TAG}" \
    $BUILD_ARGS \
    --push \
    .

echo "✅ Successfully built and pushed ${IMAGE_NAME}:${TAG} for ${PLATFORMS}"
