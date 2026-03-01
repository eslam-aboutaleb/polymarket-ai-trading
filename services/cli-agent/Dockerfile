FROM python:3.11-slim

WORKDIR /app

# Install system dependencies including GitHub CLI
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY services/cli-agent/pyproject.toml services/cli-agent/README.md ./
RUN pip install --no-cache-dir -e .

# Copy proto files and generate
COPY services/proto/ proto/
RUN mkdir -p src && python -m grpc_tools.protoc -Iproto --python_out=src --grpc_python_out=src proto/analysis.proto

# Copy source code
COPY services/cli-agent/src/ ./src/
COPY services/cli-agent/mcp-config.json .
COPY services/cli-agent/mcps/ ./mcps/
COPY services/mcps/research/ ./services/mcps/research/

# Set Python path
ENV PYTHONPATH=/app/src

EXPOSE 50052

CMD ["python", "-m", "server"]
