"""End-to-end tests for every MCP tool that writes to Nautobot.

Each test calls the real MCP tool, then verifies the resulting state with the raw REST API
(independent of the MCP code). Run against the local throwaway Nautobot only:

    docker compose -f tests/integration/docker-compose.yml up -d --build
    .venv/bin/python -m pytest tests/integration -v
"""

import pytest
import pytest_asyncio

from conftest import NET, RUN, uid

VID = 2000 + int(RUN, 16) % 1500  # per-run VLAN IDs

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def rest(api, path, **params):
    r = await api.get(path, params={"depth": 1, **params})
    r.raise_for_status()
    return r.json()


async def one(api, endpoint, **params):
    data = await rest(api, f"{endpoint}/", **params)
    assert data["count"] == 1, f"expected 1 {endpoint} for {params}, got {data['count']}"
    return data["results"][0]


# ── IPAM ─────────────────────────────────────────────────────────────


async def test_create_ip_address_with_interface(tools, api, seed):
    out = await tools.ok("nautobot_create_ip_address", address=f"{NET}.1.10/24",
                         device="mcpt-dev1", interface="Gi1", description="t")
    ip = await one(api, "ipam/ip-addresses", address=f"{NET}.1.10/24")
    assert ip["status"]["name"] == "Active" and out["ip_address"]["assigned_to"] == "mcpt-dev1:Gi1"
    assoc = await rest(api, "ipam/ip-address-to-interface/", ip_address=ip["id"])
    assert assoc["results"][0]["interface"]["id"] == seed["interface"]["id"]


async def test_delete_ip_address(tools, api, seed):
    await tools.ok("nautobot_create_ip_address", address=f"{NET}.1.11/24", device="mcpt-dev1", interface="Gi1")
    await tools.ok("nautobot_delete_ip_address", address=f"{NET}.1.11/24")
    assert (await rest(api, "ipam/ip-addresses/", address=f"{NET}.1.11/24"))["count"] == 0


async def test_update_device_ip_replaces_address(tools, api, seed):
    await tools.ok("nautobot_create_ip_address", address=f"{NET}.2.5/24", device="mcpt-dev1", interface="Gi1")
    out = await tools.ok("nautobot_update_device_ip", device="mcpt-dev1", interface="Gi1", new_ipv4=f"{NET}.3.5/24")
    assert not any(c.startswith(("ERROR", "WARNING")) for c in out["changes"]), out["changes"]
    assert (await rest(api, "ipam/ip-addresses/", address=f"{NET}.2.5/24"))["count"] == 0
    new = await one(api, "ipam/ip-addresses", address=f"{NET}.3.5/24")
    assoc = await rest(api, "ipam/ip-address-to-interface/", ip_address=new["id"])
    assert assoc["count"] == 1


async def test_create_vlan_with_location(tools, api, seed):
    out = await tools.ok("nautobot_create_vlan", vid=VID, name=uid("vlan"), location="mcpt-loc")
    assert "location_warning" not in out["vlan"], out["vlan"].get("location_warning")
    vlan = await one(api, "ipam/vlans", name=uid("vlan"))
    assert vlan["vid"] == VID
    assert (await rest(api, "ipam/vlan-location-assignments/", vlan=vlan["id"]))["count"] == 1


async def test_create_prefix_with_location(tools, api, seed):
    out = await tools.ok("nautobot_create_prefix", prefix=f"{NET}.10.0/24", location="mcpt-loc")
    assert "location_warning" not in out["prefix"], out["prefix"].get("location_warning")
    pfx = await one(api, "ipam/prefixes", prefix=f"{NET}.10.0/24")
    assert (await rest(api, "ipam/prefix-location-assignments/", prefix=pfx["id"]))["count"] == 1


async def test_assign_vrf_to_device(tools, api, seed):
    await tools.ok("nautobot_create", object_type="vrf", data={"name": uid("vrf"), "namespace": "Global"})
    await tools.ok("nautobot_assign_vrf_to_device", vrf=uid("vrf"), device="mcpt-dev1", rd="65000:1")
    vrf = await one(api, "ipam/vrfs", name=uid("vrf"))
    assert (await rest(api, "ipam/vrf-device-assignments/", vrf=vrf["id"]))["count"] == 1


# ── Generic update / create / delete ─────────────────────────────────


@pytest.mark.parametrize("object_type,identifier,updates,check", [
    ("device", "mcpt-dev1", {"serial": "SN-" + RUN}, ("dcim/devices", {"name": "mcpt-dev1"}, "serial", "SN-" + RUN)),
    ("interface", "mcpt-dev1:Gi1", {"description": "upd-" + RUN},
     ("dcim/interfaces", {"device": "mcpt-dev1", "name": "Gi1"}, "description", "upd-" + RUN)),
])
async def test_update_object(tools, api, seed, object_type, identifier, updates, check):
    await tools.ok("nautobot_update_object", object_type=object_type, identifier=identifier, updates=updates)
    endpoint, lookup, field, expected = check
    assert (await one(api, endpoint, **lookup))[field] == expected


async def test_update_object_status_by_name(tools, api, seed):
    await tools.ok("nautobot_update_object", object_type="device", identifier="mcpt-dev1", updates={"status": "Planned"})
    assert (await one(api, "dcim/devices", name="mcpt-dev1"))["status"]["name"] == "Planned"
    await tools.ok("nautobot_update_object", object_type="device", identifier="mcpt-dev1", updates={"status": "Active"})


def _generic_cases():
    """Minimal valid payloads for every nautobot_create type, using names as documented."""
    return [
        ("location_type", {"name": uid("lt"), "content_types": ["dcim.device"]}),
        ("location", {"name": uid("loc"), "location_type": "mcpt-site", "status": "Active"}),
        ("manufacturer", {"name": uid("mfr")}),
        ("device_type", {"model": uid("dt"), "manufacturer": "mcpt-mfr"}),
        ("platform", {"name": uid("plat"), "manufacturer": "mcpt-mfr"}),
        ("device", {"name": uid("dev"), "device_type": "mcpt-model", "role": "mcpt-role",
                    "location": "mcpt-loc", "status": "Active"}),
        ("interface", {"device": "mcpt-dev1", "name": uid("if"), "type": "virtual", "status": "Active"}),
        ("rack_group", {"name": uid("rg"), "location": "mcpt-loc"}),
        ("rack", {"name": uid("rack"), "status": "Active", "location": "mcpt-loc"}),
        ("namespace", {"name": uid("ns")}),
        ("prefix", {"prefix": f"{NET}.20.0/24", "status": "Active", "namespace": "Global"}),
        ("ip_address", {"address": f"{NET}.20.9/24", "status": "Active", "namespace": "Global"}),
        ("vlan_group", {"name": uid("vg")}),
        ("vlan", {"vid": VID + 1, "name": uid("vlan2"), "status": "Active"}),
        ("vrf", {"name": uid("vrf2"), "rd": "65000:" + str(int(RUN, 16) % 60000)}),
        ("route_target", {"name": "65000:" + str(int(RUN, 16) % 60000)}),
        ("service", {"name": uid("svc"), "ports": [443], "protocol": "tcp", "device": "mcpt-dev1"}),
        ("provider", {"name": uid("prov")}),
        ("circuit_type", {"name": uid("ctype")}),
        ("provider_network", {"name": uid("pnet"), "provider": uid("prov")}),
        ("circuit", {"cid": uid("cid"), "status": "Active", "provider": uid("prov"), "circuit_type": uid("ctype")}),
        ("circuit_termination", {"term_side": "A", "circuit": uid("cid"), "location": "mcpt-loc"}),
        ("tenant_group", {"name": uid("tg")}),
        ("tenant", {"name": uid("ten"), "tenant_group": uid("tg")}),
        ("cluster_type", {"name": uid("clt")}),
        ("cluster_group", {"name": uid("clg")}),
        ("cluster", {"name": uid("cl"), "cluster_type": uid("clt"), "cluster_group": uid("clg")}),
        ("virtual_machine", {"name": uid("vm"), "status": "Active", "cluster": "mcpt-cluster"}),
        ("tag", {"name": uid("tag"), "content_types": ["dcim.device"]}),
        ("role", {"name": uid("role"), "content_types": ["dcim.device"]}),
        ("status", {"name": uid("status"), "content_types": ["dcim.device"]}),
        ("contact", {"name": uid("contact")}),
        ("autonomous_system", {"asn": 4200000000 + int(RUN, 16) % 1000000, "status": "Active"}),
        ("bgp_routing_instance", {"device": uid("dev"), "autonomous_system": 64512, "status": "Active"}),
        ("bgp_peering", {"status": "Active"}),
    ]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def asn_64512(api):
    r = await api.get("plugins/bgp/autonomous-systems/", params={"asn": 64512})
    if not r.json()["results"]:
        st = (await api.get("extras/statuses/", params={"name": "Active"})).json()["results"][0]["id"]
        r2 = await api.post("plugins/bgp/autonomous-systems/", json={"asn": 64512, "status": st})
        assert r2.status_code == 201, r2.text


@pytest.mark.parametrize("object_type,data", _generic_cases(), ids=[c[0] for c in _generic_cases()])
async def test_generic_create(tools, api, seed, asn_64512, object_type, data):
    out = await tools.ok("nautobot_create", object_type=object_type, data=data)
    assert out["created"] and out["object"]["id"]


async def test_generic_bgp_peer_group(tools, api, seed, asn_64512):
    # routing_instance given as the device name; resolved by the tool
    out = await tools.ok("nautobot_create", object_type="bgp_peer_group",
                         data={"name": uid("pg"), "routing_instance": uid("dev"), "autonomous_system": 64512})
    assert out["object"]["routing_instance"]["id"]


async def test_generic_get_schema_every_type(tools):
    import server
    for t in server._OBJECT_REGISTRY:
        out = await tools(("nautobot_get_schema"), object_type=t)
        assert "fields" in out, f"{t}: {out}"


async def test_generic_delete_by_name(tools, api, seed):
    await tools.ok("nautobot_create", object_type="manufacturer", data={"name": uid("del-mfr")})
    await tools.ok("nautobot_delete", object_type="manufacturer", identifier=uid("del-mfr"))
    assert (await rest(api, "dcim/manufacturers/", name=uid("del-mfr")))["count"] == 0
    await tools.ok("nautobot_create", object_type="vlan", data={"vid": VID + 2, "name": uid("delvlan"), "status": "Active"})
    await tools.ok("nautobot_delete", object_type="vlan", identifier=str(VID + 2))
    assert (await rest(api, "ipam/vlans/", vid=VID + 2))["count"] == 0


# ── Virtualization ───────────────────────────────────────────────────


async def test_vm_create_interface_and_ip(tools, api, seed):
    vm = uid("vm-flow")
    await tools.ok("nautobot_create_virtual_machine", name=vm, cluster="mcpt-cluster", role="mcpt-role",
                   vcpus=2, memory=2048, disk=20)
    await tools.ok("nautobot_create_vm_interface", virtual_machine=vm, name="eth0", description="t")
    iface = await one(api, "virtualization/interfaces", virtual_machine=vm, name="eth0")
    assert iface["status"]["name"] == "Active"
    await tools.ok("nautobot_assign_ip_to_vm", virtual_machine=vm, interface="eth0", address=f"{NET}.30.7/24")
    vmobj = await one(api, "virtualization/virtual-machines", name=vm)
    assert vmobj["primary_ip4"] and vmobj["primary_ip4"]["address"] == f"{NET}.30.7/24"


# ── High-level BGP / interface tools ─────────────────────────────────


async def test_create_interface_with_ip(tools, api, seed):
    out = await tools.ok("nautobot_create_interface", device="mcpt-dev1", name=uid("lo"),
                         ip_address=f"{NET}.40.1/32", description="t")
    assert out["interface_action"] == "created" and out["ip_action"] == "ip_created_and_assigned"
    ip = await one(api, "ipam/ip-addresses", address=f"{NET}.40.1/32")
    assoc = await rest(api, "ipam/ip-address-to-interface/", ip_address=ip["id"])
    assert assoc["count"] == 1 and assoc["results"][0]["interface"]["display"].startswith(uid("lo"))


async def test_create_autonomous_system(tools, api, seed):
    asn = 4210000000 + int(RUN, 16) % 1000000
    out = await tools.ok("nautobot_create_autonomous_system", asn=asn, description="t")
    assert out["action"] == "created"
    assert (await one(api, "plugins/bgp/autonomous-systems", asn=asn))["status"]["name"] == "Active"


async def test_create_bgp_peer_group_and_peering(tools, api, seed, asn_64512):
    ri = await rest(api, "plugins/bgp/routing-instances/", device="mcpt-dev1")
    if not ri["count"]:
        await tools.ok("nautobot_create", object_type="bgp_routing_instance",
                       data={"device": "mcpt-dev1", "autonomous_system": 64512, "status": "Active"})
    await tools.ok("nautobot_create_interface", device="mcpt-dev1", name="Gi2", interface_type="1000base-t",
                   ip_address=f"{NET}.50.2/30")
    pg = uid("pg2")
    out = await tools.ok("nautobot_create_bgp_peer_group", name=pg, device="mcpt-dev1", remote_asn=64512,
                         source_interface="Gi2", address_families="ipv4_unicast")
    assert out["action"] == "created"
    await tools.ok("nautobot_create_ip_address", address=f"{NET}.50.1/30")
    out = await tools.ok("nautobot_create_bgp_peering", device="mcpt-dev1", local_ip=f"{NET}.50.2/30",
                         peer_ip=f"{NET}.50.1/30", peer_asn=65099, peer_group=pg)
    eps = await rest(api, "plugins/bgp/peer-endpoints/", peering=out["peering_id"])
    assert eps["count"] == 2


# ── Golden config / extras ───────────────────────────────────────────


async def test_compliance_feature_and_rule(tools, api, seed):
    await tools.ok("nautobot_create_compliance_feature", name=uid("ntp"))
    await tools.ok("nautobot_create_compliance_rule", feature=uid("ntp"), platform="mcpt-ios", match_config="ntp server")
    feat = await one(api, "plugins/golden-config/compliance-feature", name=uid("ntp"))
    assert (await rest(api, "plugins/golden-config/compliance-rule/", feature=feat["id"]))["count"] == 1


async def test_git_repository_create_update(tools, api, seed):
    out = await tools.ok("nautobot_create_git_repository", name=uid("repo"),
                         remote_url=f"https://example.invalid/{RUN}.git", provided_contents="extras.configcontext")
    rid = out["repository"]["id"]
    await tools.ok("nautobot_update_git_repository", repository_id=rid, updates={"branch": "dev"})
    assert (await rest(api, f"extras/git-repositories/{rid}/"))["branch"] == "dev"


async def test_graphql_query(tools, api, seed):
    await tools.ok("nautobot_create_graphql_query", name=uid("gq"), query="query { devices { name } }")
    assert (await rest(api, "extras/graphql-queries/", name=uid("gq")))["count"] == 1


async def test_config_context_create_update(tools, api, seed):
    out = await tools.ok("nautobot_create_config_context", name=uid("ctx"), data={"a": 1},
                         roles="mcpt-role", locations="mcpt-loc", platforms="mcpt-ios")
    cid = out["config_context"]["id"]
    await tools.ok("nautobot_update_config_context", config_context_id=cid, updates={"data": {"a": 2}})
    assert (await rest(api, f"extras/config-contexts/{cid}/"))["data"] == {"a": 2}


async def test_secrets(tools, api, seed):
    s = await tools.ok("nautobot_create_secret", name=uid("sec"), provider="environment-variable",
                       parameters={"variable": "MCPT_TOKEN"})
    g = await tools.ok("nautobot_create_secrets_group", name=uid("sg"))
    await tools.ok("nautobot_add_secret_to_group", secrets_group_id=g["secrets_group"]["id"],
                   secret_id=s["secret"]["id"], access_type="HTTP(S)", secret_type="token")
    assert (await rest(api, "extras/secrets-groups-associations/", secrets_group=g["secrets_group"]["id"]))["count"] == 1


async def test_golden_config_setting_update(tools, api, seed):
    settings = await rest(api, "plugins/golden-config/golden-config-settings/")
    assert settings["count"] >= 1, "golden config plugin should create a default setting"
    sid = settings["results"][0]["id"]
    gq = await tools.ok("nautobot_create_graphql_query", name=uid("sotagg"),
                        query="query ($device_id: ID!) { device(id: $device_id) { name } }")
    await tools.ok("nautobot_update_golden_config_setting", setting_id=sid,
                   updates={"backup_path_template": "{{obj.name}}.cfg", "sot_agg_query": gq["graphql_query"]["id"]})
    assert (await rest(api, f"plugins/golden-config/golden-config-settings/{sid}/"))["backup_path_template"] == "{{obj.name}}.cfg"


# ── Jobs ─────────────────────────────────────────────────────────────


async def test_enable_and_run_job(tools, api, seed):
    jobs = await rest(api, "extras/jobs/", q="Export Object List", limit=5)
    job = next(j for j in jobs["results"] if j["name"] == "Export Object List")
    await tools.ok("nautobot_enable_job", job_id=job["id"], enabled=True)
    ct = (await rest(api, "extras/content-types/", app_label="dcim", model="device"))["results"][0]["id"]
    out = await tools.ok("nautobot_run_job", job_id=job["id"], data={"content_type": ct})
    jr_id = out["job_result"].get("job_result", {}).get("id") or out["job_result"].get("id")
    assert jr_id, out
    res = await tools.ok("nautobot_get_job_result", job_result_id=jr_id)
    assert res["id"] == jr_id


# ── ITSM gate applies to every write tool ────────────────────────────


async def test_itsm_blocks_writes_without_cr(tools, monkeypatch):
    import server
    monkeypatch.setattr(server, "ITSM_ENABLED", True)
    monkeypatch.setattr(server, "ITSM_LAB_MODE", False)
    calls = {
        "nautobot_create_vm_interface": {"virtual_machine": "x", "name": "eth0"},
        "nautobot_assign_existing_ip_to_interface": {"address": "10.0.0.1/32", "interface": "lo", "virtual_machine": "x"},
        "nautobot_run_job": {"job_id": "x"},
        "nautobot_enable_job": {"job_id": "x"},
        "nautobot_create_interface": {"device": "x", "name": "y"},
        "nautobot_create_autonomous_system": {"asn": 65000},
        "nautobot_create_bgp_peer_group": {"name": "x", "device": "y"},
        "nautobot_create_bgp_peering": {"device": "x", "local_ip": "1.1.1.1/32", "peer_ip": "1.1.1.2/32", "peer_asn": 1},
    }
    for tool, args in calls.items():
        out = await tools(tool, **args)
        assert "ITSM" in out.get("error", ""), f"{tool} was not blocked by ITSM: {out}"


# ── Coverage for remaining update types, AFs, and failure/cleanup paths ──


async def test_update_object_remaining_types(tools, api, seed):
    await tools.ok("nautobot_create", object_type="prefix",
                   data={"prefix": f"{NET}.60.0/24", "status": "Active", "namespace": "Global"})
    await tools.ok("nautobot_create_ip_address", address=f"{NET}.60.5/24")
    await tools.ok("nautobot_update_object", object_type="ip_address", identifier=f"{NET}.60.5/24",
                   updates={"description": "u"})
    assert (await one(api, "ipam/ip-addresses", address=f"{NET}.60.5/24"))["description"] == "u"
    await tools.ok("nautobot_update_object", object_type="prefix", identifier=f"{NET}.60.0/24",
                   updates={"description": "u"})
    assert (await one(api, "ipam/prefixes", prefix=f"{NET}.60.0/24"))["description"] == "u"
    await tools.ok("nautobot_create_vlan", vid=VID + 3, name=uid("vlan-u"))
    await tools.ok("nautobot_update_object", object_type="vlan", identifier=str(VID + 3), updates={"description": "u"})
    assert (await one(api, "ipam/vlans", vid=VID + 3))["description"] == "u"
    await tools.ok("nautobot_create", object_type="vrf", data={"name": uid("vrf-u")})
    await tools.ok("nautobot_update_object", object_type="vrf", identifier=uid("vrf-u"), updates={"description": "u"})
    assert (await one(api, "ipam/vrfs", name=uid("vrf-u")))["description"] == "u"


async def test_cable_create_update_delete(tools, api, seed):
    a = await tools.ok("nautobot_create", object_type="interface",
                       data={"device": "mcpt-dev1", "name": uid("ca"), "type": "1000base-t", "status": "Active"})
    b = await tools.ok("nautobot_create", object_type="interface",
                       data={"device": "mcpt-dev1", "name": uid("cb"), "type": "1000base-t", "status": "Active"})
    c = await tools.ok("nautobot_create", object_type="cable", data={
        "termination_a_type": "dcim.interface", "termination_a_id": a["object"]["id"],
        "termination_b_type": "dcim.interface", "termination_b_id": b["object"]["id"], "status": "Connected"})
    cid = c["object"]["id"]
    await tools.ok("nautobot_update_object", object_type="cable", identifier=cid, updates={"label": "u"})
    assert (await rest(api, f"dcim/cables/{cid}/"))["label"] == "u"
    await tools.ok("nautobot_delete", object_type="cable", identifier=cid)


async def test_bgp_address_families_created(tools, api, seed):
    ri = await rest(api, "plugins/bgp/routing-instances/", device="mcpt-dev1")
    if not ri["count"]:
        await tools.ok("nautobot_create", object_type="bgp_routing_instance",
                       data={"device": "mcpt-dev1", "autonomous_system": 64512})
    pg = uid("pg-af")
    out = await tools.ok("nautobot_create_bgp_peer_group", name=pg, device="mcpt-dev1",
                         address_families="ipv4_unicast,ipv6_unicast")
    afs = await rest(api, "plugins/bgp/peer-group-address-families/", peer_group=out["id"])
    assert sorted(a["afi_safi"] for a in afs["results"]) == ["ipv4_unicast", "ipv6_unicast"]

    await tools.ok("nautobot_create_interface", device="mcpt-dev1", name=uid("gi-af"),
                   interface_type="1000base-t", ip_address=f"{NET}.70.2/30")
    await tools.ok("nautobot_create_ip_address", address=f"{NET}.70.1/30")
    p = await tools.ok("nautobot_create_bgp_peering", device="mcpt-dev1", local_ip=f"{NET}.70.2/30",
                       peer_ip=f"{NET}.70.1/30", peer_asn=65099, address_families="ipv4_unicast",
                       description="desc-" + RUN)
    eps = await rest(api, "plugins/bgp/peer-endpoints/", peering=p["peering_id"])
    local = next(e for e in eps["results"] if e.get("routing_instance"))
    assert local["description"] == "desc-" + RUN
    ep_afs = await rest(api, "plugins/bgp/peer-endpoint-address-families/", peer_endpoint=local["id"])
    assert [a["afi_safi"] for a in ep_afs["results"]] == ["ipv4_unicast"]


async def test_bgp_invalid_inputs_create_nothing(tools, api, seed):
    before = (await rest(api, "plugins/bgp/peerings/"))["count"]
    bad_afi = await tools("nautobot_create_bgp_peering", device="mcpt-dev1", local_ip=f"{NET}.70.2/30",
                          peer_ip=f"{NET}.70.1/30", peer_asn=65099, address_families="ipv4_bogus")
    assert "Invalid address_families" in bad_afi["error"]
    no_pg = await tools("nautobot_create_bgp_peering", device="mcpt-dev1", local_ip=f"{NET}.70.2/30",
                        peer_ip=f"{NET}.70.1/30", peer_asn=65099, peer_group="does-not-exist")
    assert "not found" in no_pg["error"]
    no_peer_ip = await tools("nautobot_create_bgp_peering", device="mcpt-dev1", local_ip=f"{NET}.70.2/30",
                             peer_ip=f"{NET}.71.1/30", peer_asn=65099)
    assert "Peer IP" in no_peer_ip["error"]
    assert (await rest(api, "plugins/bgp/peerings/"))["count"] == before
    bad_pg = await tools("nautobot_create_bgp_peer_group", name=uid("pg-bad"), device="mcpt-dev1", remote_asn=1)
    assert "not found" in bad_pg["error"]
    assert (await rest(api, "plugins/bgp/peer-groups/", name=uid("pg-bad")))["count"] == 0


async def test_assign_ip_to_vm_bad_interface_leaves_no_orphan_ip(tools, api, seed):
    out = await tools("nautobot_assign_ip_to_vm", virtual_machine="nope", interface="eth9", address=f"{NET}.80.1/24")
    assert "error" in out
    vm = uid("vm-orphan")
    await tools.ok("nautobot_create_virtual_machine", name=vm, cluster="mcpt-cluster")
    out = await tools("nautobot_assign_ip_to_vm", virtual_machine=vm, interface="eth9", address=f"{NET}.80.1/24")
    assert "not found" in out["error"]
    assert (await rest(api, "ipam/ip-addresses/", address=f"{NET}.80.1/24"))["count"] == 0


async def test_create_interface_is_idempotent(tools, api, seed):
    name = uid("lo-idem")
    await tools.ok("nautobot_create_interface", device="mcpt-dev1", name=name, ip_address=f"{NET}.90.1/32")
    again = await tools.ok("nautobot_create_interface", device="mcpt-dev1", name=name, ip_address=f"{NET}.90.1/32")
    assert again["interface_action"] == "already_exists"
    assert again["ip_action"] == "ip_already_exists_already_assigned"


# ── VM platform / custom fields / IPv6 primary / update_object on VMs ──


async def test_vm_full_lifecycle(tools, api, seed):
    vm = uid("vm-full")
    await tools.ok("nautobot_create", object_type="prefix",
                   data={"prefix": f"fd00:{RUN[:4]}::/64", "status": "Active", "namespace": "Global"})
    await tools.ok("nautobot_create_virtual_machine", name=vm, cluster="mcpt-cluster", platform="Rocky Linux",
                   tenant="mcpt-tenant", custom_fields={"app_id": "APP5927", "owner_team": "cdn"})
    obj = await one(api, "virtualization/virtual-machines", name=vm)
    assert obj["platform"]["name"] == "Rocky Linux" and obj["tenant"]["name"] == "mcpt-tenant"
    assert obj["custom_fields"] == {"app_id": "APP5927", "owner_team": "cdn"}

    await tools.ok("nautobot_create_vm_interface", virtual_machine=vm, name="eth0")
    v4 = await tools.ok("nautobot_assign_ip_to_vm", virtual_machine=vm, interface="eth0", address=f"{NET}.100.5/24")
    v6 = await tools.ok("nautobot_assign_ip_to_vm", virtual_machine=vm, interface="eth0", address=f"fd00:{RUN[:4]}::5/64")
    assert v4["primary"] == "primary_ip4" and v6["primary"] == "primary_ip6"
    obj = await one(api, "virtualization/virtual-machines", name=vm)
    assert obj["primary_ip4"]["address"] == f"{NET}.100.5/24"
    assert obj["primary_ip6"]["address"] == f"fd00:{RUN[:4]}::5/64"

    # update_object on a VM: platform by name, custom field partial update, primary_ip6 by address
    await tools.ok("nautobot_update_object", object_type="virtual_machine", identifier=vm,
                   updates={"platform": "mcpt-ios", "custom_fields": {"app_id": "APP0001"}, "vcpus": 4})
    obj = await one(api, "virtualization/virtual-machines", name=vm)
    assert obj["platform"]["name"] == "mcpt-ios" and obj["vcpus"] == 4
    assert obj["custom_fields"] == {"app_id": "APP0001", "owner_team": "cdn"}, "partial CF update wiped other fields"

    # Existing IPv6 already on eth0 (the reported case): set it primary via update_object by address
    await tools.ok("nautobot_update_object", object_type="virtual_machine", identifier=vm, updates={"primary_ip6": None})
    assert (await one(api, "virtualization/virtual-machines", name=vm))["primary_ip6"] is None
    await tools.ok("nautobot_update_object", object_type="virtual_machine", identifier=vm,
                   updates={"primary_ip6": f"fd00:{RUN[:4]}::5/64"})
    assert (await one(api, "virtualization/virtual-machines", name=vm))["primary_ip6"]["address"] == f"fd00:{RUN[:4]}::5/64"

    # update_object on a VM interface, "vm:interface" identifier
    await tools.ok("nautobot_update_object", object_type="vm_interface", identifier=f"{vm}:eth0",
                   updates={"description": "u", "mtu": 9000})
    iface = await one(api, "virtualization/interfaces", virtual_machine=vm, name="eth0")
    assert iface["description"] == "u" and iface["mtu"] == 9000


async def test_update_object_every_registry_type_resolves(tools, api, seed):
    """Every nautobot_create type is accepted by update_object (no 'Unknown object_type')."""
    import server
    for t in server._OBJECT_REGISTRY:
        out = await tools("nautobot_update_object", object_type=t, identifier="00000000-0000-0000-0000-000000000000",
                          updates={"description": "x"})
        assert "Unknown object_type" not in out.get("error", ""), t


async def test_vm_create_bad_custom_fields_json(tools, seed):
    out = await tools("nautobot_create_virtual_machine", name=uid("vm-bad"), cluster="mcpt-cluster",
                      custom_fields="{not json")
    assert "Invalid custom_fields JSON" in out["error"]


# ── Attach existing (shared / Anycast) IPs without creating them ─────


async def test_assign_existing_anycast_ip_to_many_interfaces(tools, api, seed):
    await tools.ok("nautobot_create", object_type="prefix",
                   data={"prefix": f"fd01:{RUN[:4]}::/64", "status": "Active", "namespace": "Global"})
    v4, v6 = f"{NET}.110.241/32", f"fd01:{RUN[:4]}::241/128"
    await tools.ok("nautobot_create_ip_address", address=v4)
    await tools.ok("nautobot_create_ip_address", address=v6)
    ip_count_before = (await rest(api, "ipam/ip-addresses/", q=f"{NET}.110."))["count"]

    vms = [uid(f"anycast{i}") for i in range(3)]
    for vm in vms:
        await tools.ok("nautobot_create_virtual_machine", name=vm, cluster="mcpt-cluster")
        await tools.ok("nautobot_create_vm_interface", virtual_machine=vm, name="lo")
        # Both families onto the same lo, from the same shared IP objects
        a = await tools.ok("nautobot_assign_existing_ip_to_interface", address=v4, virtual_machine=vm, interface="lo")
        b = await tools.ok("nautobot_assign_existing_ip_to_interface", address=v6, virtual_machine=vm,
                           interface="lo", set_primary=True)
        assert a["action"] == "assigned" and b["action"] == "assigned" and b["primary"] == "primary_ip6"
        iface = await one(api, "virtualization/interfaces", virtual_machine=vm, name="lo")
        assoc = await rest(api, "ipam/ip-address-to-interface/", vm_interface=iface["id"])
        assert sorted(x["ip_address"]["display"] for x in assoc["results"]) == sorted([v4, v6])
        assert (await one(api, "virtualization/virtual-machines", name=vm))["primary_ip6"]["address"] == v6

    # One IP object shared by all three interfaces; no new IPs created
    ip4 = await one(api, "ipam/ip-addresses", address=v4)
    assert (await rest(api, "ipam/ip-address-to-interface/", ip_address=ip4["id"]))["count"] == 3
    assert (await rest(api, "ipam/ip-addresses/", q=f"{NET}.110."))["count"] == ip_count_before

    # Idempotent re-run
    again = await tools.ok("nautobot_assign_existing_ip_to_interface", address=v4, virtual_machine=vms[0], interface="lo")
    assert again["action"] == "already_assigned"


async def test_assign_existing_ip_to_device_interface(tools, api, seed):
    addr = f"{NET}.111.9/24"
    await tools.ok("nautobot_create_ip_address", address=addr)
    await tools.ok("nautobot_create_interface", device="mcpt-dev1", name=uid("lo-ex"))
    out = await tools.ok("nautobot_assign_existing_ip_to_interface", address=addr, device="mcpt-dev1",
                         interface=uid("lo-ex"))
    assert out["action"] == "assigned"
    ip = await one(api, "ipam/ip-addresses", address=addr)
    assoc = await rest(api, "ipam/ip-address-to-interface/", ip_address=ip["id"])
    assert assoc["results"][0]["interface"]["display"].startswith(uid("lo-ex"))


async def test_assign_existing_ip_never_creates(tools, api, seed):
    missing = f"{NET}.112.1/32"
    vm = uid("anycast-none")
    await tools.ok("nautobot_create_virtual_machine", name=vm, cluster="mcpt-cluster")
    await tools.ok("nautobot_create_vm_interface", virtual_machine=vm, name="lo")
    out = await tools("nautobot_assign_existing_ip_to_interface", address=missing, virtual_machine=vm, interface="lo")
    assert "not found" in out["error"]
    assert (await rest(api, "ipam/ip-addresses/", address=missing))["count"] == 0
    both = await tools("nautobot_assign_existing_ip_to_interface", address=missing, virtual_machine=vm,
                       device="mcpt-dev1", interface="lo")
    assert "exactly one" in both["error"]
    bad_if = await tools("nautobot_assign_existing_ip_to_interface", address=f"{NET}.111.9/24",
                         virtual_machine=vm, interface="nope")
    assert "not found" in bad_if["error"]
