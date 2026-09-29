"""Fixtures for write-tool integration tests against a LOCAL throwaway Nautobot.

Start it first:
    docker compose -f tests/integration/docker-compose.yml up -d --build

These tests create and delete real records, so they refuse to run against anything
other than localhost.
"""

import json
import os
import uuid
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio

LOCAL_URL = os.environ.get("TEST_NAUTOBOT_URL", "http://127.0.0.1:18080")
LOCAL_TOKEN = os.environ.get("TEST_NAUTOBOT_TOKEN", "0123456789abcdef0123456789abcdef01234567")

if urlparse(LOCAL_URL).hostname not in ("127.0.0.1", "localhost"):
    raise RuntimeError(f"Refusing to run write tests against non-local Nautobot: {LOCAL_URL}")

# Must be set before the server modules are imported (they read env at import time).
os.environ["NAUTOBOT_URL"] = LOCAL_URL
os.environ["NAUTOBOT_TOKEN"] = LOCAL_TOKEN
os.environ["NAUTOBOT_VERIFY_SSL"] = "false"
os.environ["ITSM_ENABLED"] = "false"

import server  # noqa: E402

RUN = uuid.uuid4().hex[:6]  # suffix so reruns don't collide
# Per-run IPv4 /16 (10.x.0.0/16) so addresses never collide with a previous run.
NET = f"10.{int(RUN, 16) % 250 + 1}"


def uid(name: str) -> str:
    return f"mcpt-{name}-{RUN}"


class Tools:
    """Call MCP tools the same way a client does and decode the JSON result."""

    def __init__(self, mcp):
        self.mcp = mcp

    async def __call__(self, tool: str, **args):
        res = await self.mcp.call_tool(tool, args)
        blocks = res[0] if isinstance(res, tuple) else res
        return json.loads(blocks[0].text)

    async def ok(self, tool: str, **args):
        out = await self(tool, **args)
        assert "error" not in out, f"{tool} failed: {out['error']}"
        return out


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def tools():
    try:
        httpx.get(f"{LOCAL_URL}/api/status/", headers={"Authorization": f"Token {LOCAL_TOKEN}"}, timeout=10).raise_for_status()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Local Nautobot not reachable at {LOCAL_URL}: {e}")
    return Tools(server.mcp)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def api():
    """Raw REST client for seeding and verifying state (independent of the MCP code)."""
    async with httpx.AsyncClient(
        base_url=f"{LOCAL_URL}/api/",
        headers={"Authorization": f"Token {LOCAL_TOKEN}", "Accept": "application/json"},
        timeout=60,
    ) as c:
        yield c


async def _get_or_create(api, endpoint: str, lookup: dict, body: dict) -> dict:
    r = await api.get(f"{endpoint}/", params={**lookup, "depth": 1})
    r.raise_for_status()
    if r.json()["results"]:
        return r.json()["results"][0]
    r = await api.post(f"{endpoint}/", json=body)
    assert r.status_code == 201, f"seed {endpoint} failed: {r.text}"
    return r.json()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def seed(api):
    """Baseline objects most write tools need. Idempotent across runs."""
    s: dict = {}
    s["status"] = (await api.get("extras/statuses/", params={"name": "Active"})).json()["results"][0]
    s["location_type"] = await _get_or_create(
        api, "dcim/location-types", {"name": "mcpt-site"},
        {"name": "mcpt-site", "content_types": [
            "dcim.device", "ipam.vlan", "ipam.prefix", "virtualization.cluster", "dcim.rack",
            "dcim.rackgroup", "circuits.circuittermination", "ipam.vlangroup"]},
    )
    s["location"] = await _get_or_create(
        api, "dcim/locations", {"name": "mcpt-loc"},
        {"name": "mcpt-loc", "location_type": s["location_type"]["id"], "status": s["status"]["id"]},
    )
    s["manufacturer"] = await _get_or_create(api, "dcim/manufacturers", {"name": "mcpt-mfr"}, {"name": "mcpt-mfr"})
    s["device_type"] = await _get_or_create(
        api, "dcim/device-types", {"model": "mcpt-model"},
        {"model": "mcpt-model", "manufacturer": s["manufacturer"]["id"]},
    )
    s["role"] = await _get_or_create(
        api, "extras/roles", {"name": "mcpt-role"},
        {"name": "mcpt-role", "content_types": ["dcim.device", "virtualization.virtualmachine", "ipam.ipaddress"]},
    )
    s["platform"] = await _get_or_create(
        api, "dcim/platforms", {"name": "mcpt-ios"},
        {"name": "mcpt-ios", "manufacturer": s["manufacturer"]["id"], "network_driver": "cisco_ios"},
    )
    s["device"] = await _get_or_create(
        api, "dcim/devices", {"name": "mcpt-dev1"},
        {"name": "mcpt-dev1", "device_type": s["device_type"]["id"], "role": s["role"]["id"],
         "location": s["location"]["id"], "status": s["status"]["id"], "platform": s["platform"]["id"]},
    )
    s["interface"] = await _get_or_create(
        api, "dcim/interfaces", {"device_id": s["device"]["id"], "name": "Gi1"},
        {"device": s["device"]["id"], "name": "Gi1", "type": "1000base-t", "status": s["status"]["id"]},
    )
    s["cluster_type"] = await _get_or_create(api, "virtualization/cluster-types", {"name": "mcpt-ct"}, {"name": "mcpt-ct"})
    s["cluster"] = await _get_or_create(
        api, "virtualization/clusters", {"name": "mcpt-cluster"},
        {"name": "mcpt-cluster", "cluster_type": s["cluster_type"]["id"]},
    )
    s["namespace"] = (await api.get("ipam/namespaces/", params={"name": "Global"})).json()["results"][0]
    # Parent container for this run's addresses (Nautobot requires a parent prefix for IPs).
    s["container"] = await _get_or_create(
        api, "ipam/prefixes", {"prefix": f"{NET}.0.0/16"},
        {"prefix": f"{NET}.0.0/16", "status": s["status"]["id"], "namespace": s["namespace"]["id"], "type": "container"},
    )
    s["asn"] = await _get_or_create(
        api, "plugins/bgp/autonomous-systems", {"asn": 64512}, {"asn": 64512, "status": s["status"]["id"]},
    )
    return s
