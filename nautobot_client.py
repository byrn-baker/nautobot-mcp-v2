"""Nautobot API client — GraphQL for reads, REST for writes.

Compatible with Nautobot 2.x and 3.x:
  - Auto-detects instance version via /api/status/
  - On 3.x, appends exclude_m2m=False to REST GET requests so M2M fields
    (like IP-to-interface assignments) are included in responses.
  - The ip-address-to-interface endpoint works on both 2.x and 3.x.
"""

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Cache for resolved UUIDs (type:name -> uuid)
_id_cache: dict[str, str] = {}


class NautobotError(Exception):
    pass


class NautobotAuthError(NautobotError):
    pass


class NautobotConnectionError(NautobotError):
    pass


class NautobotClient:
    def __init__(self):
        self.url = os.environ["NAUTOBOT_URL"].rstrip("/")
        self.token = os.environ["NAUTOBOT_TOKEN"]
        verify = os.environ.get("NAUTOBOT_VERIFY_SSL", "false").lower() == "true"
        timeout = int(os.environ.get("NAUTOBOT_TIMEOUT", "60"))

        self.http = httpx.AsyncClient(
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            verify=verify,
            timeout=timeout,
        )

        # Version detection (lazy, cached after first call)
        self._nautobot_version: Optional[str] = None
        self._is_v3: Optional[bool] = None

    async def close(self):
        await self.http.aclose()

    # ── Version Detection ────────────────────────────────────────────

    async def detect_version(self) -> str:
        """Detect the Nautobot instance version via /api/status/. Cached after first call."""
        if self._nautobot_version is not None:
            return self._nautobot_version

        try:
            resp = await self.http.get(f"{self.url}/api/status/")
            if resp.status_code == 200:
                data = resp.json()
                self._nautobot_version = data.get("nautobot-version", "2.0.0")
            else:
                self._nautobot_version = "2.0.0"
        except Exception:
            self._nautobot_version = "2.0.0"  # Assume 2.x if detection fails

        try:
            major = int(self._nautobot_version.split(".")[0])
            self._is_v3 = major >= 3
        except (ValueError, IndexError):
            self._is_v3 = False

        logger.info(f"Detected Nautobot version: {self._nautobot_version} (v3={self._is_v3})")
        return self._nautobot_version

    async def is_v3(self) -> bool:
        """Return True if the Nautobot instance is version 3.x or later."""
        if self._is_v3 is None:
            await self.detect_version()
        return self._is_v3

    # ── GraphQL ──────────────────────────────────────────────────────

    async def graphql(
        self, query: str, variables: Optional[dict] = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables

        try:
            resp = await self.http.post(f"{self.url}/api/graphql/", json=body)
        except httpx.ConnectError as e:
            raise NautobotConnectionError(
                f"Nautobot API unreachable at {self.url}: {e}"
            )
        except httpx.TimeoutException:
            raise NautobotConnectionError(
                f"Nautobot query timed out after {self.http.timeout.read}s."
            )

        self._check_http(resp)
        data = resp.json()

        if "errors" in data:
            msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
            raise NautobotError(f"Nautobot GraphQL error: {msgs}")

        return data.get("data", {})

    # ── REST ─────────────────────────────────────────────────────────

    async def rest_get(
        self, endpoint: str, params: Optional[dict] = None
    ) -> dict[str, Any]:
        return await self._rest("GET", endpoint, params=params)

    async def rest_post(self, endpoint: str, data: dict) -> dict[str, Any]:
        return await self._rest("POST", endpoint, json_body=data)

    async def rest_patch(self, endpoint: str, data: dict) -> dict[str, Any]:
        return await self._rest("PATCH", endpoint, json_body=data)

    async def rest_delete(self, endpoint: str) -> dict[str, Any]:
        return await self._rest("DELETE", endpoint)

    async def rest_list(
        self, endpoint: str, params: Optional[dict] = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """GET a list endpoint with pagination."""
        p = dict(params or {})
        p["limit"] = limit
        p["offset"] = offset
        return await self._rest("GET", endpoint, params=p)

    async def _rest(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict[str, Any]:
        url = f"{self.url}/api/{endpoint.strip('/')}/"

        # Nautobot 3.x excludes M2M fields by default. Always request them
        # on GET so that IP-to-interface assignments and similar relations
        # are visible in responses.
        if method == "GET" and await self.is_v3():
            if params is None:
                params = {}
            params.setdefault("exclude_m2m", "False")

        try:
            resp = await self.http.request(method, url, params=params, json=json_body)
        except httpx.ConnectError as e:
            raise NautobotConnectionError(
                f"Nautobot API unreachable at {self.url}: {e}"
            )
        except httpx.TimeoutException:
            raise NautobotConnectionError(
                f"Nautobot request timed out after {self.http.timeout.read}s."
            )

        self._check_http(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()

    # ── ID Resolution ────────────────────────────────────────────────

    async def resolve_id(self, object_type: str, name: str) -> str:
        """Resolve a human-readable name to a Nautobot UUID.

        Supported object_type values:
          status, role, location, device, platform, namespace,
          vlan_group, tenant, interface (use "device_name:iface_name")
        """
        cache_key = f"{object_type}:{name}"
        if cache_key in _id_cache:
            return _id_cache[cache_key]

        query_map: dict[str, str] = {
            "status": '{{ statuses(name: "{}") {{ id }} }}'.format(_esc(name)),
            "role": '{{ roles(name: "{}") {{ id }} }}'.format(_esc(name)),
            "location": '{{ locations(name: "{}") {{ id }} }}'.format(_esc(name)),
            "location_type": '{{ location_types(name: "{}") {{ id }} }}'.format(_esc(name)),
            "device": '{{ devices(name: "{}") {{ id }} }}'.format(_esc(name)),
            "device_type": '{{ device_types(model: "{}") {{ id }} }}'.format(_esc(name)),
            "manufacturer": '{{ manufacturers(name: "{}") {{ id }} }}'.format(_esc(name)),
            "platform": '{{ platforms(name: "{}") {{ id }} }}'.format(_esc(name)),
            "tenant": '{{ tenants(name: "{}") {{ id }} }}'.format(_esc(name)),
            "tenant_group": '{{ tenant_groups(name: "{}") {{ id }} }}'.format(_esc(name)),
            "vlan_group": '{{ vlan_groups(name: "{}") {{ id }} }}'.format(_esc(name)),
            "vrf": '{{ vrfs(name: "{}") {{ id }} }}'.format(_esc(name)),
            "cluster": '{{ clusters(name: "{}") {{ id }} }}'.format(_esc(name)),
            "cluster_type": '{{ cluster_types(name: "{}") {{ id }} }}'.format(_esc(name)),
            "cluster_group": '{{ cluster_groups(name: "{}") {{ id }} }}'.format(_esc(name)),
            "virtual_machine": '{{ virtual_machines(name: "{}") {{ id }} }}'.format(_esc(name)),
            "provider": '{{ providers(name: "{}") {{ id }} }}'.format(_esc(name)),
            "circuit_type": '{{ circuit_types(name: "{}") {{ id }} }}'.format(_esc(name)),
            "tag": '{{ tags(name: "{}") {{ id }} }}'.format(_esc(name)),
            "route_target": '{{ route_targets(name: "{}") {{ id }} }}'.format(_esc(name)),
            "circuit": '{{ circuits(cid: "{}") {{ id }} }}'.format(_esc(name)),
            "ip_address": '{{ ip_addresses(address: "{}") {{ id }} }}'.format(_esc(name)),
            "provider_network": '{{ provider_networks(name: "{}") {{ id }} }}'.format(_esc(name)),
            # BGP models plugin: ASN by number, routing instance by device name
            "bgp_routing_instance": '{{ bgp_routing_instances(device: "{}") {{ id }} }}'.format(_esc(name)),
        }
        if object_type == "autonomous_system":
            if not str(name).isdigit():
                raise NautobotError(f"Autonomous system must be an ASN number, got '{name}'")
            query_map["autonomous_system"] = f"{{ autonomous_systems(asn: {int(name)}) {{ id }} }}"

        if object_type == "namespace":
            # Namespaces use REST — not always in GraphQL
            resp = await self.rest_get("ipam/namespaces", {"name": name})
            results = resp.get("results", [])
            if not results:
                raise NautobotError(f"Namespace '{name}' not found in Nautobot.")
            uid = results[0]["id"]
            _id_cache[cache_key] = uid
            return uid

        if object_type == "interface":
            # Expect "DeviceName:InterfaceName"
            if ":" not in name:
                raise NautobotError(
                    "Interface identifier must be 'device_name:interface_name'"
                )
            dev, iface = name.split(":", 1)
            q = '{{ interfaces(device: "{}", name: "{}") {{ id }} }}'.format(
                _esc(dev), _esc(iface)
            )
            data = await self.graphql(q)
            items = _first_list(data)
            if not items:
                raise NautobotError(
                    f"Interface '{iface}' on device '{dev}' not found in Nautobot."
                )
            uid = items[0]["id"]
            _id_cache[cache_key] = uid
            return uid

        if object_type not in query_map:
            raise NautobotError(f"Cannot resolve object type '{object_type}'")

        data = await self.graphql(query_map[object_type])
        items = _first_list(data)
        if not items:
            raise NautobotError(
                f"{object_type.replace('_', ' ').title()} '{name}' not found in Nautobot."
            )
        uid = items[0]["id"]
        _id_cache[cache_key] = uid
        return uid

    # ── Helpers ───────────────────────────────────────────────────────

    async def get(self, path: str, params: Optional[dict] = None) -> dict[str, Any]:
        """Low-level GET using the full path (e.g. '/api/dcim/devices/')."""
        url = f"{self.url}{path}" if path.startswith("/") else f"{self.url}/{path}"

        # Nautobot 3.x: include M2M fields
        if await self.is_v3():
            if params is None:
                params = {}
            params.setdefault("exclude_m2m", "False")

        try:
            resp = await self.http.get(url, params=params)
        except httpx.ConnectError as e:
            raise NautobotConnectionError(f"Nautobot API unreachable: {e}")
        except httpx.TimeoutException:
            raise NautobotConnectionError("Nautobot request timed out.")
        self._check_http(resp)
        return resp.json()

    async def post(self, path: str, json: Optional[dict] = None) -> dict[str, Any]:
        """Low-level POST using the full path (e.g. '/api/ipam/ip-addresses/')."""
        url = f"{self.url}{path}" if path.startswith("/") else f"{self.url}/{path}"
        try:
            resp = await self.http.post(url, json=json)
        except httpx.ConnectError as e:
            raise NautobotConnectionError(f"Nautobot API unreachable: {e}")
        except httpx.TimeoutException:
            raise NautobotConnectionError("Nautobot request timed out.")
        self._check_http(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()

    def _check_http(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise NautobotAuthError(
                "Nautobot authentication failed. Verify NAUTOBOT_TOKEN is correct."
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            raise NautobotError(
                f"Nautobot REST API error ({resp.status_code}): {detail}"
            )


def _esc(s: str) -> str:
    """Escape a string for embedding in a GraphQL query."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _first_list(data: dict) -> list:
    """Return the first list value from a GraphQL response dict."""
    for v in data.values():
        if isinstance(v, list):
            return v
    return []
