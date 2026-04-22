# syntax=docker/dockerfile:1
# Multi-stage build: compile frontend with Node.js, then install Python backend

################################
# STAGE 1: Frontend build
################################
FROM node:20-slim AS frontend_build

WORKDIR /frontend

# Copy package manifests first for better layer caching
COPY src/frontend/package.json src/frontend/package-lock.json ./

# Install Node.js dependencies (use ci for reproducible installs)
RUN npm ci

# Copy the rest of the frontend source
COPY src/frontend/ ./

# Build the frontend (output goes to ./build/)
RUN NODE_OPTIONS="--max-old-space-size=4096" npm run build

################################
# STAGE 2: Python backend
################################
FROM python:3.13-slim AS runtime

WORKDIR /app

# Install system dependencies required by langflow
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install --no-install-recommends -y \
        build-essential \
        git \
        curl \
        libpq5 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the full source (backend + any custom components the user added)
COPY src/ ./src/
COPY pyproject.toml README.md ./

# Copy the built frontend on top of the source tree.
# langflow's main.py (in langflow-base) resolves the frontend path as:
#   Path(__file__).parent / "frontend"
# When installed, __file__ resolves to:
#   /usr/local/lib/python3.13/site-packages/langflow/main.py
# The top-level langflow package (src/backend/langflow) shares the same
# namespace, so placing the frontend here causes it to land at
# site-packages/langflow/frontend/ — exactly where langflow looks.
COPY --from=frontend_build /frontend/build/ /app/src/backend/langflow/frontend/

# Install the lfx sub-package first (langflow-base depends on it)
RUN pip install --no-cache-dir ./src/lfx

# Install langflow-base (the core backend with main.py and the frontend path logic)
RUN pip install --no-cache-dir ./src/backend/base

# Install the top-level langflow package in editable mode so the user's
# custom function is picked up from source without reinstalling
RUN pip install --no-cache-dir -e .

# langflow's main.py resolves the frontend path as Path(__file__).parent / "frontend".
# After installation, __file__ is site-packages/langflow/main.py, so we must ensure
# the built frontend exists at site-packages/langflow/frontend/.
# We copy it there explicitly to guarantee the path exists regardless of install mode.
RUN SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])") \
    && mkdir -p "${SITE_PACKAGES}/langflow/frontend" \
    && cp -r /app/src/backend/langflow/frontend/. "${SITE_PACKAGES}/langflow/frontend/"

EXPOSE 7860

ENV LANGFLOW_HOST=0.0.0.0
ENV LANGFLOW_PORT=7860

CMD ["python", "-m", "langflow", "run", "--host", "0.0.0.0", "--port", "7860"]
