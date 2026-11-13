FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install UV for fast package management
RUN pip install --no-cache-dir uv==0.13.5

# Copy project files
COPY services/llm-chain/pyproject.toml services/llm-chain/uv.lock services/llm-chain/README.md ./
COPY services/llm-chain/src/ src/
COPY services/mcps/research/ services/mcps/research/
# Shared packages used by both AI services (polymarket_mcp and
# polymarket_settings). Kept as a single implementation so
# llm-chain and cli-agent cannot drift apart.
COPY services/shared/ shared/

# Install dependencies
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# Copy and compile proto files
COPY services/proto/ proto/
RUN python -m grpc_tools.protoc \
    -I proto/ \
    --python_out=. \
    --grpc_python_out=. \
    proto/analysis.proto

# Set PYTHONPATH. /app gives the `src` package; /app/shared gives
# `polymarket_mcp` and `polymarket_settings`.
ENV PYTHONPATH=/app:/app/shared

# Create SQLite RAG data directory
RUN mkdir -p /app/data/chroma

# Expose gRPC port
EXPOSE 50051

# Run the server
CMD ["python", "src/server.py"]
