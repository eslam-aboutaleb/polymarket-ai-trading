#!/usr/bin/env python3
"""Regenerate backend stubs with the project's frozen grpcio-tools dependency.

Run from any directory: uv run --project backend --frozen python backend/generate_proto.py
Pass --check to fail if the checked-in stubs differ from the generated output.
"""

import argparse
import tempfile
from pathlib import Path

from grpc_tools import protoc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = root / "backend/app/grpc_clients"
    with tempfile.TemporaryDirectory() as tmp:
        result = protoc.main(
            [
                "grpc_tools.protoc",
                f"-I{root / 'services/proto'}",
                f"--python_out={tmp}",
                f"--grpc_python_out={tmp}",
                str(root / "services/proto/analysis.proto"),
            ]
        )
        if result:
            raise SystemExit(result)
        for filename in ("analysis_pb2.py", "analysis_pb2_grpc.py"):
            generated = (
                (Path(tmp) / filename)
                .read_text()
                .replace(
                    "import analysis_pb2 as analysis__pb2",
                    "from app.grpc_clients import analysis_pb2 as analysis__pb2",
                )
            )
            target = destination / filename
            if args.check:
                if target.read_text() != generated:
                    raise SystemExit(
                        f"{filename} differs; regenerate with the frozen backend environment"
                    )
            else:
                target.write_text(generated)


if __name__ == "__main__":
    main()
