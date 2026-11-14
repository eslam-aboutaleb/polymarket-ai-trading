"""Exercise the regenerated serializers and handlers over a real loopback RPC."""

from concurrent.futures import ThreadPoolExecutor

import grpc

from app.grpc_clients import analysis_pb2 as messages
from app.grpc_clients import analysis_pb2_grpc as rpc


def test_generated_health_rpc_roundtrip():
    class Service(rpc.AnalysisServiceServicer):
        def HealthCheck(self, request, context):
            return messages.HealthResponse(
                healthy=True,
                service_name="contract-test",
                version="1",
                capabilities={"research": "read-only"},
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        server = grpc.server(executor)
        rpc.add_AnalysisServiceServicer_to_server(Service(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                result = rpc.AnalysisServiceStub(channel).HealthCheck(
                    messages.HealthRequest(), timeout=5
                )
                assert result.healthy
                assert result.service_name == "contract-test"
                assert dict(result.capabilities) == {"research": "read-only"}
        finally:
            server.stop(0).wait()
