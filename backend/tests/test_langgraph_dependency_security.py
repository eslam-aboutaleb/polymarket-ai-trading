"""Dependency security regression: untrusted identifiers stay one path segment."""

import httpx
import pytest
from langgraph_sdk.client import SyncLangGraphClient


@pytest.mark.parametrize(
    "identifier", ["../store", "thread/../../assistants", "thread?token=other#fragment"]
)
def test_v3_stream_encodes_assistant_identifier_before_authenticated_graph_request(identifier):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"nodes": [], "edges": []})

    with httpx.Client(
        base_url="https://langgraph.invalid",
        transport=httpx.MockTransport(receive),
        headers={"x-api-key": "disposable-test-key"},
    ) as transport:
        client = SyncLangGraphClient(transport)
        stream = client.threads.stream("test-thread", assistant_id=identifier)
        assert stream.agent.get_tree() == {"nodes": [], "edges": []}
    assert len(requests) == 1
    url = requests[0].url
    assert url.host == "langgraph.invalid"
    assert url.query == b"" and not url.fragment
    assert url.raw_path.startswith(b"/assistants/") and url.raw_path.endswith(b"/graph")
    assert b"/" not in url.raw_path.removeprefix(b"/assistants/").removesuffix(b"/graph")
