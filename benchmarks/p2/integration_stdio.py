#!/usr/bin/env python3
"""P2: real stdio MCP issue #5 with Core PR #4 and legacy Core parent.

All cases invoke the actual MCP PR build, and real OSV through CONNECT proxy.
There are no product changes in this runner, no agent calls and no repin.
"""
from __future__ import annotations
import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import threading
import time
import traceback

import jsonschema
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.d0.stdio_d0 import WireClient, selected_content  # noqa: E402
sys.path.insert(0, str(ROOT / "mcp" / "scripts"))
from smoke_live_proxy_osv import Proxy  # noqa: E402

MCP_SHA = "922173f8ed06f9cbb342b349c435d17594a39de5"
PINNED_CORE_SHA = "ba35b37c311fba13937ad4b2f46f760d57d9ab11"
PARENT_CORE_SHA = "14cae77162478349a8de69db680ab656404daace"
PR_CORE_SHA = "21f626ba8ad2e9f59b07fe14b3abbfb4420a3436"
CANARY = "P15_DO_NOT_LEAK_SYNTHETIC_MARKER"
ORIGINAL_BLUEPRINT = ROOT / "mcp" / "src" / "deploom_mcp" / "blueprint.py"
SCHEMA_PATH = ROOT / "mcp" / "contracts" / "upgrade-blueprint.schema.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def git_sha(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def make_tarball(version: str) -> bytes:
    # Actual deterministic npm tarball; no install runs during this experiment.
    buffer = io.BytesIO()
    with tarfile.open(mode="w:gz", fileobj=buffer, compresslevel=6) as archive:
        payload = json.dumps({"name": "lodash", "version": version, "main": "index.js"}, sort_keys=True).encode()
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        info.mode = 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(payload))
        code = b"module.exports = { integrationFixture: true };\n"
        info = tarfile.TarInfo("package/index.js")
        info.size = len(code)
        info.mode = 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(code))
    return buffer.getvalue()


class RegistryFixture:
    versions = ("4.17.20", "4.18.1")

    def __init__(self, mode: str):
        self.mode = mode
        self.tarballs = {v: make_tarball(v) for v in self.versions}
        self.hits: list[dict] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self):
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                outer.hits.append({"path": self.path.split("?", 1)[0], "range": self.headers.get("Range")})
                path = self.path.split("?", 1)[0]
                if path in ("/npm/lodash", "/npm/lodash/"):
                    body = json.dumps(outer.metadata(), sort_keys=True).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                for version, value in outer.tarballs.items():
                    if path == f"/npm/lodash/-/lodash-{version}.tgz":
                        ran = self.headers.get("Range") == "bytes=0-0"
                        payload = value[:1] if ran else value
                        self.send_response(206 if ran else 200)
                        if ran:
                            self.send_header("Content-Range", f"bytes 0-0/{len(value)}")
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def registry(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}/npm"

    def metadata(self) -> dict:
        base = self.registry
        versions = {}
        for version in self.versions:
            uri = f"{base}/lodash/-/lodash-{version}.tgz"
            if self.mode == "foreign":
                uri = f"https://registry.npmjs.org/lodash/-/lodash-{version}.tgz?token={CANARY}"
            elif self.mode == "wrong-prefix":
                uri = f"{base.rsplit('/npm', 1)[0]}/outside/lodash/-/lodash-{version}.tgz"
            elif self.mode == "missing-dist" and version == "4.18.1":
                uri = ""
            raw = self.tarballs[version]
            versions[version] = {"name": "lodash", "version": version,
                "dist": {"tarball": uri, "shasum": hashlib.sha1(raw).hexdigest(),
                         "integrity": "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode()},
                "engines": {"node": ">=14"}}
            if not uri:
                versions[version]["dist"].pop("tarball")
        return {"name": "lodash", "dist-tags": {"latest": "4.18.1"}, "versions": versions,
                "time": {"4.17.20": "2020-04-15T00:00:00.000Z", "4.18.1": "2026-10-15T00:00:00.000Z"}}

    def independently_verify_tarball(self):
        url = self.registry + "/lodash/-/lodash-4.18.1.tgz"
        response = requests.get(url, timeout=4, proxies={"http": None, "https": None})
        response.raise_for_status()
        raw = self.tarballs["4.18.1"]
        assert response.content == raw
        expected = self.metadata()["versions"]["4.18.1"]["dist"]["integrity"]
        observed = "sha512-" + base64.b64encode(hashlib.sha512(response.content).digest()).decode()
        assert observed == expected
        assert observed != "sha512-" + base64.b64encode(hashlib.sha512(b"tampered").digest()).decode()
        ranged = requests.get(url, headers={"Range": "bytes=0-0"}, timeout=4, proxies={"http": None, "https": None})
        assert ranged.status_code == 206 and ranged.content == raw[:1]
        assert self.hits[-1]["range"] == "bytes=0-0"
        return {"sha256": digest(raw), "sha512IntegrityVerified": True, "httpStatus": response.status_code,
                "rangedHttpStatus": ranged.status_code, "bytes": len(raw),
                "note": "Independent fixture HTTP verification; Core Draft does not install/download tarballs"}

    def __exit__(self, *_):
        assert self.server is not None and self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=4)


def make_project(path: Path) -> dict[str, str]:
    path.mkdir(parents=True, exist_ok=False)
    data = {"name": "deploom-p15-local-registry", "version": "1.0.0", "private": True,
            "dependencies": {"lodash": "4.17.20"}}
    lock = {"name": data["name"], "version": data["version"], "lockfileVersion": 3,
            "requires": True, "packages": {"": {"name": data["name"], "version": "1.0.0",
            "dependencies": data["dependencies"]}, "node_modules/lodash": {"version": "4.17.20"}}}
    dump(path / "package.json", data)
    dump(path / "package-lock.json", lock)
    return {x.name: digest(x.read_bytes()) for x in path.iterdir()}


def identity() -> dict:
    heads = {"mcp": git_sha(ROOT / "mcp"), "embeddedCore": git_sha(ROOT / "mcp/upstream/deploom"),
             "coreParent": git_sha(ROOT / "core-parent"), "corePR": git_sha(ROOT / "core-pr")}
    assert heads == {"mcp": MCP_SHA, "embeddedCore": PINNED_CORE_SHA,
                     "coreParent": PARENT_CORE_SHA, "corePR": PR_CORE_SHA}, heads
    assert (ROOT / "core-pr/registry_candidate_diagnostics.py").is_file()
    assert (ROOT / "mcp/release-manifest.json").is_file()
    return heads


def _legacy_unused_prototype_adapter_patch() -> dict:
    raw = ORIGINAL_BLUEPRINT.read_bytes()
    s = raw.decode("utf-8")
    old = '''                "scanState": row.get("scanState"),
            },
        })'''
    new = '''                "scanState": row.get("scanState"),
            },
            **({"registryCandidateDiagnostics": row["registryCandidateDiagnostics"]}
               if isinstance(row.get("registryCandidateDiagnostics"), dict) else {}),
            **({"noTargetDiagnosticCode": row["noTargetDiagnosticCode"]}
               if isinstance(row.get("noTargetDiagnosticCode"), str) else {}),
        })'''
    assert s.count(old) == 1, "PROTOTYPE_ADAPTER_ROW_ANCHOR_CHANGED"
    s = s.replace(old, new)
    old = '        "dependencies": dependencies,'
    new = '''        "dependencies": dependencies,
        **({"registryDiagnostics": plan["registryDiagnostics"]}
           if isinstance(plan.get("registryDiagnostics"), dict) else {}),'''
    assert s.count(old) == 1, "PROTOTYPE_ADAPTER_AGGREGATE_ANCHOR_CHANGED"
    s = s.replace(old, new)
    import ast
    ast.parse(s)
    ORIGINAL_BLUEPRINT.write_text(s, encoding="utf-8")
    return {"originalSha256": digest(raw), "patchedSha256": digest(ORIGINAL_BLUEPRINT.read_bytes()),
            "patchType": "TEMPORARY_LOCAL_MCP_PROJECTION_ONLY; NOT A PRODUCT COMMIT",
            "changedAdapterBehavior": ["registryCandidateDiagnostics", "noTargetDiagnosticCode", "registryDiagnostics"]}


def extend_schema(schema: dict) -> dict:
    """Prototype explicit JSON Schema, not an MCP versioned contract change."""
    schema = copy.deepcopy(schema)
    props = schema["properties"]["dependencies"]["items"]["properties"]
    props["registryCandidateDiagnostics"] = {"type": "object", "required": ["schemaVersion", "authority", "examined", "accepted", "rejected", "rejectionCounts", "allUpgradeCandidatesRejected"],
        "properties": {"schemaVersion": {"const": 1}, "authority": {"const": "STRUCTURAL_REGISTRY_EVIDENCE_ONLY"},
            "examined": {"type": "integer", "minimum": 0}, "accepted": {"type": "integer", "minimum": 0},
            "rejected": {"type": "integer", "minimum": 0},
            "rejectionCounts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
            "rejectionSamples": {"type": "array", "maxItems": 4},
            "allUpgradeCandidatesRejected": {"type": "boolean"}}}
    props["noTargetDiagnosticCode"] = {"type": "string", "enum": ["NO_INSTALLABLE_ARTIFACT", "NO_PUBLISHED_UPGRADE_CANDIDATE", "UNRESOLVED_TARGET_CONSTRAINT_OR_POLICY", "CANDIDATE_CAP"]}
    schema["properties"]["registryDiagnostics"] = {"type": "object", "properties": {
        "examinedVersions": {"type": "integer", "minimum": 0},
        "rejectedVersions": {"type": "integer", "minimum": 0},
        "noInstallableArtifactRows": {"type": "integer", "minimum": 0}}}
    return schema


def run_case(out: Path, *, core_version: str, mode: str, proxy_uri: str,
             proxy: Proxy, proto: bool, schema: dict) -> dict:
    folder = out / "cases" / ("prototype" if proto else core_version) / mode
    folder.mkdir(parents=True, exist_ok=False)
    result = {"coreVersion": core_version, "mode": mode, "prototypeAdapter": proto,
              "status": "NOT_RUN", "createdUtc": datetime.now(timezone.utc).isoformat()}
    try:
        with RegistryFixture(mode) as registry:
            metadata = registry.metadata()
            dump(folder / "fixture-packument.json", metadata)
            tarball = registry.independently_verify_tarball()
            dump(folder / "fixture-artifact-proof.json", tarball)
            initial = make_project(folder / "project")
            proxy.clear()
            env = dict(os.environ)
            env.update({"HTTP_PROXY": proxy_uri, "HTTPS_PROXY": proxy_uri,
                        "http_proxy": proxy_uri, "https_proxy": proxy_uri,
                        "ALL_PROXY": "", "all_proxy": "", "NO_PROXY": "localhost,127.0.0.1",
                        "no_proxy": "localhost,127.0.0.1", "DEPLOOM_MCP_USE_SYSTEM_PROXY": "true",
                        "DEPLOOM_MCP_STATE_DIR": str(folder / "managed-state"),
                        "DEPLOOM_PLANNING_CACHE_DIR": str(folder / "planning-cache"),
                        "DEPLOOM_MCP_GENERATOR_PATH": str(ROOT / ("core-pr" if core_version == "pr" else "core-parent") / "dependency_live_roadmap_generator.py")})
            client = WireClient(env, folder, seconds=135)
            try:
                init = client.request(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "deploom-p15-integration", "version": "0.1"}})
                dump(folder / "initialize.json", init)
                client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                tools = client.request(2, "tools/list", {})
                dump(folder / "tools-list.json", tools)
                names = [x["name"] for x in tools.get("tools", [])]
                assert all(x in names for x in ("get_upgrade_plan", "explain_conflict", "evaluate_assignment"))
                params = {"project_path": str(folder / "project"), "registry": registry.registry,
                          "use_system_proxy": True, "target_level": "yellow", "min_lag_ok_pct": 80,
                          "deadline_seconds": 100, "draft_max_candidates": 8,
                          "include_prerelease": False, "target_mode": "advisory",
                          "objective": "latest-compatible-in-approved-lines",
                          "mutable_packages": ["lodash"], "output_dir": str(folder / "planner")}
                rpc = client.request(3, "tools/call", {"name": "get_upgrade_plan", "arguments": params})
                payload = selected_content(rpc)
                dump(folder / "compact.json", payload)
                artifacts = payload.get("artifacts") or {}
                paths = {k: Path(v) for k,v in artifacts.items() if k in ("blueprint", "manifest", "plan") and isinstance(v, str)}
                assert {"blueprint", "manifest", "plan"}.issubset(paths), paths
                for f in paths.values():
                    assert f.is_file() and f.resolve().is_relative_to(folder.resolve())
                blueprint = json.loads(paths["blueprint"].read_text(encoding="utf-8"))
                plan = json.loads(paths["plan"].read_text(encoding="utf-8"))
                manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
                dump(folder / "full-blueprint.json", blueprint)
                dump(folder / "core-plan.json", plan)
                dump(folder / "core-manifest.json", manifest)
                jsonschema.validate(blueprint, schema)
                assert not proto, "PROTOTYPE_MUST_NEVER_RUN_IN_P2"
                rows = [x for p in plan.get("proposals") or [] for x in p.get("rows") or []]
                assert len(rows) == len(blueprint["dependencies"]) == 1
                origrow = rows[0]
                newrow = blueprint["dependencies"][0]
                assert blueprint["status"]["authority"] == "PLANNING_ONLY"
                assert blueprint["status"]["verificationStatus"] == "NOT_VERIFIED"
                assert blueprint["status"]["compatibility"] == "UNKNOWN"
                assert blueprint["status"]["parallelExecutionSafety"] == "UNPROVEN"
                assert newrow["status"] in ("PROPOSED", "NO_TARGET", "UNKNOWN_SECURITY", "POLICY_SATISFIED", "CANDIDATE_SEARCH_TRUNCATED", "UNKNOWN_METADATA", "BLOCKED")
                assert origrow.get("status", "").upper().replace("-", "_") == newrow["status"] or (origrow.get("status")=="ok" and newrow["status"]=="POLICY_SATISFIED")
                counts = blueprint["summary"]
                assert counts.get("securityKnown") == counts.get("securityTotal") == 1, counts
                assert all(digest((folder / "project" / n).read_bytes()) == h for n,h in initial.items())
                expected_plan_keys = ("registryDiagnostics", "registryCandidateDiagnostics", "noTargetDiagnosticCode")
                diag = origrow.get("registryCandidateDiagnostics")
                aggr = plan.get("registryDiagnostics")
                if core_version == "pr":
                    assert isinstance(diag, dict) and isinstance(aggr, dict), "PR_CORE_DIAGNOSTICS_MISSING"
                    raw_diag = json.dumps({"diag": diag, "aggregate": aggr}, ensure_ascii=False)
                    assert CANARY not in raw_diag, "SYNTHETIC_TOKEN_LEAK_IN_NEW_DIAGNOSTIC"
                    assert "tarballUrl" not in raw_diag and "access_token=" not in raw_diag
                    assert diag["schemaVersion"] == 1 and diag["authority"] == "STRUCTURAL_REGISTRY_EVIDENCE_ONLY"
                    assert diag["examined"] == len(RegistryFixture.versions)
                    assert diag["examined"] == diag["accepted"] + diag["rejected"]
                    assert diag["physicalProbe"]["unprobed"] is True
                    if mode == "matching":
                        assert diag["acceptedUpgradeCandidates"] >= 1 and diag["rejectedUpgradeCandidates"] == 0
                        assert origrow.get("noTargetDiagnosticCode") is None
                    elif mode == "foreign":
                        assert diag["acceptedUpgradeCandidates"] == 0
                        assert diag["rejectionCounts"]["FOREIGN_TARBALL_ORIGIN"] == diag["rejected"]
                        if origrow.get("status") == "no-target":
                            assert origrow.get("noTargetDiagnosticCode") == "NO_INSTALLABLE_ARTIFACT"
                    elif mode == "missing-dist":
                        assert diag["rejectionCounts"]["MISSING_DIST_TARBALL"] >= 1
                    elif mode == "wrong-prefix":
                        assert diag["rejectionCounts"]["BAD_REGISTRY_PATH"] >= 1
                else:
                    assert not any(k in plan for k in ("registryDiagnostics",))
                    assert not any(k in origrow for k in expected_plan_keys[1:])
                compact_row = payload["dependencies"][0]
                if core_version == "pr":
                    assert newrow.get("registryCandidateDiagnostics") == diag
                    assert newrow.get("noTargetDiagnosticCode") == origrow.get("noTargetDiagnosticCode")
                    assert blueprint.get("registryDiagnostics") == aggr
                    assert payload.get("registryDiagnostics") == aggr
                    assert compact_row.get("noTargetDiagnosticCode") == origrow.get("noTargetDiagnosticCode")
                    projected = compact_row.get("registryCandidateDiagnostics") or {}
                    for count_field in ("examined", "accepted", "rejected", "upgradeCandidates",
                                        "acceptedUpgradeCandidates", "rejectedUpgradeCandidates"):
                        assert projected.get(count_field) == diag.get(count_field), count_field
                    assert projected.get("rejectionCounts") == diag.get("rejectionCounts")
                    assert projected.get("evidenceRef") == "#/dependencies/0/registryCandidateDiagnostics"
                    assert not any(k in projected for k in ("configuredRegistry", "metadataRequestBase",
                                                                 "rejectedArtifactOrigins", "rejectionSamples", "physicalProbe"))
                    assert CANARY not in json.dumps(blueprint, ensure_ascii=False)
                    assert CANARY not in json.dumps(payload, ensure_ascii=False)
                    assert "?token=" not in json.dumps(payload, ensure_ascii=False)
                else:
                    assert "registryCandidateDiagnostics" not in newrow
                    assert "noTargetDiagnosticCode" not in newrow
                    assert "registryDiagnostics" not in blueprint
                    assert "registryCandidateDiagnostics" not in compact_row
                    assert "noTargetDiagnosticCode" not in compact_row
                    assert "registryDiagnostics" not in payload
                # Only the public OSV service is reachable via this proxy.
                connections = proxy.snapshot()
                assert connections.get("api.osv.dev", 0) >= 1, "MCP_DID_NOT_USE_REAL_OSV_CONNECT_PROXY"
                assert not any(x.get("path", "").endswith(".tgz") for x in registry.hits[2:]), "DRAFT_SHOULD_NOT_DOWNLOAD_ARTIFACT"
                result.update({"status": "PASS", "schemaValid": True, "typedPrototypeSchemaValid": None,
                    "corePlanStatus": origrow.get("status"), "blueprintRowStatus": newrow.get("status"),
                    "coreTarget": origrow.get("target"), "blueprintTarget": newrow.get("target"),
                    "securityKnown": counts.get("securityKnown"), "securityTotal": counts.get("securityTotal"),
                    "targetEvidenceComplete": ((payload.get("targetAssessment") or {}).get("aggregate") or {}).get("postPlanSecurityUnknown") == 0,
                    "diagnosticPresentInCorePlan": core_version == "pr",
                    "diagnosticPresentInBlueprint": "registryCandidateDiagnostics" in newrow,
                    "noTargetCodeInCorePlan": origrow.get("noTargetDiagnosticCode"),
                    "noTargetCodeInBlueprint": newrow.get("noTargetDiagnosticCode"),
                    "coreCounts": aggr, "coreDiagnostic": diag,
                    "expectedMcpProjectionGap": False,
                    "compactDiagnosticPresent": bool(compact_row.get("registryCandidateDiagnostics")),
                    "connectProxyCounts": connections, "metadataHttpRequests": sum(x.get("path")=="/npm/lodash" for x in registry.hits),
                    "projectUnchanged": True, "rawRpcRequestSha256": digest((folder / "jsonrpc-requests.raw.jsonl").read_bytes()),
                    "rawRpcResponseSha256": digest((folder / "jsonrpc-responses.raw.jsonl").read_bytes()),
                    "blueprintSha256": digest(paths["blueprint"].read_bytes()),
                    "corePlanSha256": digest(paths["plan"].read_bytes())})
            finally:
                client.close()
    except Exception as e:
        result.update({"status": "FAIL", "errorType": type(e).__name__, "errorCode": str(e)[:180], "traceback": traceback.format_exc()[-1800:]})
    dump(folder / "P15-case-result.json", result)
    return result


def main(out: Path):
    out.mkdir(parents=True, exist_ok=False)
    source_blob = ORIGINAL_BLUEPRINT.read_bytes()
    started = time.monotonic()
    report = {"status": "NOT_CONFIRMED", "startedUtc": datetime.now(timezone.utc).isoformat(),
              "noAgents": True, "noRepoWrites": True, "coreMainUnchanged": True,
              "adapterPrototypeNeverCommitted": True, "cases": []}
    try:
        report["sourceIdentity"] = identity()
        report["sourceBlobs"] = {"mcpBlueprintSha256": digest(source_blob),
            "prGeneratorSha256": digest((ROOT / "core-pr" / "dependency_live_roadmap_generator.py").read_bytes()),
            "parentGeneratorSha256": digest((ROOT / "core-parent" / "dependency_live_roadmap_generator.py").read_bytes()),
            "mcpContractSha256": digest(SCHEMA_PATH.read_bytes())}
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        with Proxy() as proxy:
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            try:
                proxy_uri = f"http://127.0.0.1:{proxy.server_address[1]}"
                query = {"package": {"name": "lodash", "ecosystem": "npm"}, "version": "4.17.20"}
                dump(out / "live-osv-preflight-query.json", query)
                response = requests.post("https://api.osv.dev/v1/query", json=query,
                                         proxies={"http": proxy_uri, "https": proxy_uri}, timeout=25)
                response.raise_for_status()
                (out / "live-osv-preflight-response.json").write_bytes(response.content)
                assert len(response.json().get("vulns") or []) > 0, "REAL_OSV_CONTROL_HAS_NO_KNOWN_FINDINGS"
                report["liveOsvPreflight"] = {"httpStatus": response.status_code,
                    "records": len(response.json().get("vulns") or []), "responseSha256": digest(response.content)}
                for base, mode in [("parent", "matching"), ("parent", "foreign"),
                                   ("pr", "matching"), ("pr", "foreign"),
                                   ("pr", "wrong-prefix"), ("pr", "missing-dist")]:
                    item = run_case(out, core_version=base, mode=mode, proxy_uri=proxy_uri,
                                    proxy=proxy, proto=False, schema=schema)
                    report["cases"].append(item)
                # P2 is a real MCP PR, not a temporary monkey-patched prototype.
                # The original source must remain byte-identical throughout.
            finally:
                proxy.shutdown()
                proxy.server_close()
        # Status/target equivalence between PR and its immediate parent, per fixture.
        pairs = {v["mode"]: (v, next(w for w in report["cases"] if w["mode"]==v["mode"] and w["coreVersion"]=="pr" and not w["prototypeAdapter"]))
                 for v in report["cases"] if v["coreVersion"] == "parent"}
        report["statusEquivalence"] = {mode: {
            "parentStatus": a.get("blueprintRowStatus"), "prStatus": b.get("blueprintRowStatus"),
            "parentTarget": a.get("blueprintTarget"), "prTarget": b.get("blueprintTarget"),
            "sameStatusAndTarget": (a.get("blueprintRowStatus"),a.get("blueprintTarget")) == (b.get("blueprintRowStatus"),b.get("blueprintTarget"))}
            for mode, (a,b) in pairs.items()}
        report["status"] = "P2_MCP_FULL_AND_COMPACT_PROJECTION_VALIDATED" if (
             all(x["status"]=="PASS" for x in report["cases"])
             and all(x["sameStatusAndTarget"] for x in report["statusEquivalence"].values())) else "P15_INTEGRATION_TEST_FAILED"
    except Exception as e:
        report.update({"status": "P15_HARNESS_FAILED", "errorType": type(e).__name__,
                       "errorCode": str(e)[:200], "traceback": traceback.format_exc()[-2500:]})
    finally:
        report["adapterRestoredByteIdentical"] = digest(ORIGINAL_BLUEPRINT.read_bytes()) == digest(source_blob)
        report["endedUtc"] = datetime.now(timezone.utc).isoformat()
        report["wallSeconds"] = round(time.monotonic() - started, 2)
        dump(out / "P15-RESULTS.json", report)
        files={}
        for path in sorted(out.rglob("*")):
            if not path.is_file() or path.name == "SHA256-MANIFEST.json":
                continue
            # Exclude state/cache and planner internal data; the published plan, blueprint and raw RPC are saved separately.
            if any(x in path.parts for x in ("managed-state", "planning-cache", "planner")):
                continue
            files[str(path.relative_to(out))] = digest(path.read_bytes())
        dump(out / "SHA256-MANIFEST.json", {"files": files, "fileCount": len(files), "selfExcluded": True})
        print(json.dumps({"status": report["status"], "cases": [(x["coreVersion"],x["mode"],x["prototypeAdapter"],x["status"],x.get("corePlanStatus"),x.get("noTargetCodeInCorePlan")) for x in report["cases"]],
            "adapterRestored": report["adapterRestoredByteIdentical"],
            "statusEquivalence": report.get("statusEquivalence")}, ensure_ascii=False))
    return 0 if report["status"] == "P2_MCP_FULL_AND_COMPACT_PROJECTION_VALIDATED" else 2


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args=parser.parse_args()
    raise SystemExit(main(args.out))
