#!/usr/bin/env python3
"""One real, free (no LLM) DepLoom MCP/Core D0 calibration with raw JSON-RPC.

Run only on public lodash@4.17.20. External HTTP CONNECT is observed by
an instrumented localhost proxy from pinned DepLoom/mcp test utilities.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import requests

# Import only a networking test fixture from the exact pinned source checkout.
SCRIPT_ROOT = Path(__file__).resolve().parents[2]
PINNED_MCP = SCRIPT_ROOT / "mcp"
sys.path.insert(0, str(PINNED_MCP / "scripts"))
from smoke_live_proxy_osv import Proxy  # type: ignore[import-not-found]  # noqa: E402

MCP_SHA = "71a6bc1da0c14a20c89147db24aaa4f87aeb3528"
CORE_SHA = "ba35b37c311fba13937ad4b2f46f760d57d9ab11"
BUNDLE_BLOB_SHA = "f21ff9846ee75a0c50d4ea3caf09fac623238793"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def create_calibration(project: Path) -> dict[str, str]:
    project.mkdir(parents=True, exist_ok=False)
    obj = {"name": "deploom-cheapbench-calibration", "version": "1.0.0", "private": True,
           "dependencies": {"lodash": "4.17.20"}}
    lock = {"name": obj["name"], "version": "1.0.0", "lockfileVersion": 3, "requires": True,
            "packages": {"": {"name": obj["name"], "version": "1.0.0", "dependencies": obj["dependencies"]},
                         "node_modules/lodash": {"version": "4.17.20"}}}
    # Same logical project/lock as CheapBench v0.2 (formatting is not a semantic difference).
    dump(project / "package.json", obj)
    dump(project / "package-lock.json", lock)
    return {p.name: sha(p.read_bytes()) for p in project.iterdir()}


class WireClient:
    def __init__(self, env: dict[str, str], out: Path, seconds: float = 120):
        self.out = out
        self.deadline = time.monotonic() + seconds
        self.events: queue.Queue[dict] = queue.Queue()
        self.requests = (out / "jsonrpc-requests.raw.jsonl").open("wb")
        self.responses = (out / "jsonrpc-responses.raw.jsonl").open("wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "deploom_mcp"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env,
                                     start_new_session=True, bufsize=0)
        self._reader = threading.Thread(target=self._collect, daemon=True)
        self._reader.start()

    def _collect(self) -> None:
        try:
            for line in self.proc.stdout:
                self.responses.write(line)
                self.responses.flush()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    event = {"bad_json_stdout": True}
                self.events.put(event)
        finally:
            self.events.put({"_eof": True})

    def send(self, object_: dict) -> None:
        raw = (json.dumps(object_, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self.requests.write(raw)
        self.requests.flush()
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def request(self, request_id: int, method: str, params: dict | None = None) -> dict:
        event: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            event["params"] = params
        self.send(event)
        while True:
            rem = self.deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("MCP_STDIO_TIMEOUT")
            try:
                value = self.events.get(timeout=min(rem, 1.5))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError("MCP_PROCESS_EXITED")
                continue
            if value.get("bad_json_stdout"):
                raise RuntimeError("MCP_INVALID_STDOUT_JSON")
            if value.get("_eof"):
                raise RuntimeError("MCP_CLOSED_STDOUT")
            if value.get("id") == request_id:
                if "error" in value:
                    raise RuntimeError("MCP_JSON_RPC_ERROR:" + str(value["error"].get("code", "unknown")))
                return value["result"]

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)
        self._reader.join(timeout=3)
        self.requests.close()
        self.responses.close()


def selected_content(result: dict) -> dict:
    if bool(result.get("isError", result.get("is_error", False))):
        raise RuntimeError("MCP_TOOL_RETURNED_ERROR")
    value = result.get("structuredContent", result.get("structured_content"))
    if value is None:
        value = json.loads(result["content"][0]["text"])
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise RuntimeError("MCP_TOOL_RESPONSE_NOT_OK")
    return value


def read_only_product_identity() -> dict[str, str]:
    commands = [
        ["git", "-C", str(PINNED_MCP), "rev-parse", "HEAD"],
        ["git", "-C", str(PINNED_MCP / "upstream/deploom"), "rev-parse", "HEAD"],
    ]
    heads = [subprocess.check_output(c, text=True).strip() for c in commands]
    if heads != [MCP_SHA, CORE_SHA]:
        raise RuntimeError("PINNED_PRODUCT_IDENTITY_MISMATCH")
    source = PINNED_MCP / "upstream/deploom/dependency_live_roadmap_generator.py"
    git_blob_sha = subprocess.check_output(
        ["git", "-C", str(PINNED_MCP / "upstream/deploom"), "hash-object", source.name],
        text=True).strip()
    if git_blob_sha != BUNDLE_BLOB_SHA:
        raise RuntimeError("PINNED_CORE_GENERATOR_GIT_BLOB_MISMATCH")
    return {"mcpGitSha": heads[0], "coreGitSha": heads[1],
            "coreGeneratorGitBlobSha": git_blob_sha,
            "coreGeneratorSha256": sha(source.read_bytes())}


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {"status": "D0_NOT_CONFIRMED", "startedUtc": datetime.now(timezone.utc).isoformat(),
              "noAgentCalls": True, "publicCalibrationOnly": True, "route": "LOCAL_HTTP_CONNECT_PROXY"}
    try:
        result.update(read_only_product_identity())
        initial = create_calibration(out / "calibration-project")
        with Proxy() as proxy:
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            try:
                proxy_uri = f"http://127.0.0.1:{proxy.server_address[1]}"
                # Independent live OSV request, through the same real HTTP proxy.
                query = {"package": {"name": "lodash", "ecosystem": "npm"}, "version": "4.17.20"}
                dump(out / "osv-query.json", query)
                preflight = requests.post("https://api.osv.dev/v1/query", json=query,
                                          proxies={"http": proxy_uri, "https": proxy_uri}, timeout=25)
                preflight.raise_for_status()
                response_bytes = preflight.content
                (out / "osv-response.raw.json").write_bytes(response_bytes)
                data = preflight.json()
                vuln_records = data.get("vulns") or []
                if not isinstance(vuln_records, list) or not vuln_records:
                    raise RuntimeError("KNOWN_VULNERABILITY_NOT_IN_OSV")
                result["independentLiveOsv"] = {
                    "httpStatus": preflight.status_code, "vulnerabilityRecords": len(vuln_records),
                    "responseSha256": sha(response_bytes),
                    "vulnIds": [x.get("id") for x in vuln_records if isinstance(x, dict)]}
                proxy.clear()  # don't count independent query as Core transport evidence
                env = dict(os.environ)
                env.update({"HTTP_PROXY": proxy_uri, "HTTPS_PROXY": proxy_uri,
                            "http_proxy": proxy_uri, "https_proxy": proxy_uri,
                            "ALL_PROXY": "", "all_proxy": "", "NO_PROXY": "localhost,127.0.0.1",
                            "no_proxy": "localhost,127.0.0.1",
                            "DEPLOOM_MCP_USE_SYSTEM_PROXY": "true",
                            "DEPLOOM_MCP_STATE_DIR": str(out / "managed-state"),
                            "DEPLOOM_PLANNING_CACHE_DIR": str(out / "planning-cache")})
                client = WireClient(env, out, seconds=130)
                try:
                    initialize = client.request(1, "initialize", {
                        "protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "deploom-d0-raw-github-action", "version": "0.1"}})
                    dump(out / "initialize-result.json", initialize)
                    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    listing = client.request(2, "tools/list", {})
                    dump(out / "tools-list.json", listing)
                    found = {row.get("name") for row in listing.get("tools", [])}
                    required = {"get_upgrade_plan", "evaluate_assignment", "explain_conflict"}
                    if not required.issubset(found):
                        raise RuntimeError("MCP_REQUIRED_TOOL_NAMES_MISSING")
                    args = {
                        "project_path": str(out / "calibration-project"), "target_level": "yellow",
                        "min_lag_ok_pct": 80, "deadline_seconds": 90, "include_prerelease": False,
                        "use_system_proxy": True, "registry": "https://registry.npmjs.org",
                        "target_mode": "advisory", "objective": "latest-compatible-in-approved-lines",
                        "mutable_packages": ["lodash"], "draft_max_candidates": 3,
                        "output_dir": str(out / "planner")}
                    rpc = client.request(3, "tools/call", {"name": "get_upgrade_plan", "arguments": args})
                    payload = selected_content(rpc)
                    dump(out / "compact-blueprint.json", payload)
                    path = Path((payload.get("artifacts") or {}).get("blueprint") or "")
                    if not path.is_file() or not path.resolve().is_relative_to(out.resolve()):
                        raise RuntimeError("FULL_BLUEPRINT_NOT_READABLE")
                    bp = json.loads(path.read_bytes())
                    (out / "full-blueprint.json").write_bytes(path.read_bytes())
                    metadata = bp.get("summary") or {}
                    current_known = metadata.get("securityKnown", 0)
                    security_total = metadata.get("securityTotal", 0)
                    statuses = metadata.get("statusCounts") or {}
                    post = ((payload.get("targetAssessment") or {}).get("aggregate") or {})
                    result.update({
                        "mcpToolNames": sorted(found), "mcpDraftStatus": (payload.get("status") or {}).get("draftStatus"),
                        "mcpAuthority": (payload.get("status") or {}).get("authority"),
                        "fullBlueprintSha256": sha(path.read_bytes()),
                        "mcpSecurityKnown": current_known, "mcpSecurityTotal": security_total,
                        "mcpStatusCounts": statuses, "postPlanSecurityKnown": post.get("postPlanSecurityKnown"),
                        "postPlanSecurityTotal": post.get("postPlanSecurityTotal"),
                        "postPlanSecurityUnknown": post.get("postPlanSecurityUnknown"),
                        "mcpProxyConnects": proxy.snapshot(),
                        "inputsUnchanged": all(sha((out / "calibration-project" / n).read_bytes()) == h for n, h in initial.items())
                    })
                    if not (result["mcpDraftStatus"] == "DRAFT_READY" and current_known == security_total == 1
                            and statuses.get("PROPOSED", 0) >= 1 and post.get("postPlanSecurityUnknown") == 0
                            and result["mcpProxyConnects"].get("api.osv.dev", 0) > 0
                            and result["mcpProxyConnects"].get("registry.npmjs.org", 0) > 0
                            and result["inputsUnchanged"]):
                        raise RuntimeError("D0_LIVE_EVIDENCE_INCOMPLETE")
                    result["status"] = "D0_LIVE_STDIO_RPC_OSV_CONFIRMED"
                finally:
                    client.close()
            finally:
                proxy.shutdown()
                proxy.server_close()
                thread.join(5)
    except Exception as exc:
        result["errorType"] = type(exc).__name__
        result["errorCode"] = (str(exc).split(":", 1)[0])[:110]
    finally:
        result["finishedUtc"] = datetime.now(timezone.utc).isoformat()
        result["wallSeconds"] = round(time.monotonic()-started, 3)
        dump(out / "D0-result.json", result)
        hashes = {}
        for p in sorted(out.rglob("*")):
            if p.is_file() and p.name != "SHA256-MANIFEST.json":
                # Don't include managed planner caches/state (may carry raw env in future).
                if any(n in p.parts for n in ("managed-state", "planning-cache", "planner")):
                    continue
                hashes[str(p.relative_to(out))] = sha(p.read_bytes())
        dump(out / "SHA256-MANIFEST.json", {"files": hashes, "fileCount": len(hashes), "selfExcluded": True})
        print(json.dumps({k: result.get(k) for k in
               ("status", "mcpGitSha", "coreGitSha", "mcpSecurityKnown", "mcpSecurityTotal",
                "mcpStatusCounts", "postPlanSecurityUnknown", "mcpProxyConnects", "errorType", "errorCode")},
               ensure_ascii=False))
    return 0 if result["status"] == "D0_LIVE_STDIO_RPC_OSV_CONFIRMED" else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    raise SystemExit(main(args.out))