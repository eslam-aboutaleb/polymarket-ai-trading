FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install UV
RUN pip install --no-cache-dir uv==0.13.5

# Copy project files
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY database ./database
COPY proto ./proto

# Install dependencies (locked)
RUN uv sync --frozen --no-dev

# Generate gRPC stubs from proto
RUN uv run --frozen --no-dev python3 -m grpc_tools.protoc \
    -Iproto \
    --python_out=app/grpc_clients \
    --grpc_python_out=app/grpc_clients \
    proto/analysis.proto

# Run migrations and start server
CMD ["sh", "-c", "uv run --frozen --no-dev alembic upgrade head && uv run --frozen --no-dev uvicorn app.main:app --host 0.0.0.0 --port 8002"]
