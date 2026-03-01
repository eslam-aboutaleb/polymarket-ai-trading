FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install UV
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy project files
COPY pyproject.toml .
COPY app ./app
COPY database ./database
COPY proto ./proto

# Install dependencies
RUN uv sync

# Generate gRPC stubs from proto
RUN uv run python3 -m grpc_tools.protoc \
    -Iproto \
    --python_out=app/grpc_clients \
    --grpc_python_out=app/grpc_clients \
    proto/analysis.proto

# Run migrations and start server
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"]
