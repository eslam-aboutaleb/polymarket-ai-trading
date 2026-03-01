#!/bin/bash
# Generate Python gRPC code from proto file

PROTO_DIR="../proto"
OUT_DIR="."

python -m grpc_tools.protoc \
    -I${PROTO_DIR} \
    --python_out=${OUT_DIR} \
    --grpc_python_out=${OUT_DIR} \
    ${PROTO_DIR}/analysis.proto

echo "Generated gRPC code from analysis.proto"
