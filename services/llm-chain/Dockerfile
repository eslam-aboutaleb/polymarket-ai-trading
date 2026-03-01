FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install UV for fast package management
RUN pip install uv

# Copy project files
COPY services/llm-chain/pyproject.toml services/llm-chain/README.md ./
COPY services/llm-chain/src/ src/
COPY services/mcps/research/ services/mcps/research/

# Install dependencies
RUN uv pip install --system -e .

# Copy and compile proto files
COPY services/proto/ proto/
RUN python -m grpc_tools.protoc \
    -I proto/ \
    --python_out=. \
    --grpc_python_out=. \
    proto/analysis.proto

# Set PYTHONPATH
ENV PYTHONPATH=/app

# Expose gRPC port
EXPOSE 50051

# Run the server
CMD ["python", "src/server.py"]
