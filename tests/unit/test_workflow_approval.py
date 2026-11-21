import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from github_bot.execution.issue_executor import ApplyRejected
from github_bot.execution.workflow_approval import RunContext, build_manifest, verify_approval
from github_bot.planning.planner import Plan, PlanItem, marker, plan_hash

SHA = "a" * 40
CONTEXT = RunContext(
    source_repo="example/source",
    source_sha=SHA,
    event_sha=SHA,
    ref="refs/heads/main",
    event_name="workflow_dispatch",
    run_id=42,
    run_attempt=1,
    actor="dispatcher",
    triggering_actor="triggerer",
)
TARGET = "AIVIETNAM-AIO-luciusHo/realcheck"
ENVIRONMENT = "realcheck-issue-write"


def _plan(workspace: Path, repo: str = TARGET) -> Plan:
    plan = Plan(
        repo=repo,
        source_file="tasks.md",
        items=[
            PlanItem(
                task_id="TASK-001",
                action="CREATE",
                title="Task thử nghiệm",
                body=marker("tasks.md", "TASK-001"),
                assignees=[],
                labels=[],
            )
        ],
    )
    plan.plan_sha256 = plan_hash(plan)
    (workspace / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    return plan


def _write_manifest(workspace: Path, manifest: dict[str, object]) -> None:
    (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _approval(login: str = "reviewer", environment: str = ENVIRONMENT, state: str = "approved") -> dict[str, object]:
    return {"state": state, "environments": [{"name": environment}], "user": {"login": login}}


def _verify(workspace: Path, transport: httpx.MockTransport, context: RunContext = CONTEXT) -> str:
    return verify_approval(
        workspace,
        "plan.json",
        "manifest.json",
        context,
        actions_token="test-only",
        transport=transport,
    )


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    _plan(tmp_path)
    manifest = build_manifest(tmp_path, "plan.json", CONTEXT)
    _write_manifest(tmp_path, manifest)
    return tmp_path, manifest


@pytest.mark.parametrize(
    ("approvals", "expected"),
    [
        ([_approval()], "reviewer"),
        ([_approval("DISPATCHER")], None),
        ([_approval("TRIGGERER")], None),
        ([_approval(environment="other")], None),
        ([_approval(state="rejected")], None),
        ([_approval(), _approval("other-reviewer")], None),
        ([], None),
        ([{"state": "approved", "user": {"login": "reviewer"}}], None),
    ],
)
def test_workflow_approval_requires_one_independent_review(
    prepared: tuple[Path, dict[str, object]], approvals: object, expected: str | None,
) -> None:
    workspace, _ = prepared
    ref_path = "/repos/example/source/git/ref/heads/main"
    approval_path = "/repos/example/source/actions/runs/42/approvals"
    called_paths = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        called_paths.append(request.url.path)
        if request.url.path == ref_path:
            return httpx.Response(200, json={"ref": CONTEXT.ref, "object": {"sha": SHA, "type": "commit"}})
        assert request.url.path == approval_path
        return httpx.Response(200, json=approvals)

    transport = httpx.MockTransport(respond)
    if expected is None:
        with pytest.raises(ApplyRejected):
            _verify(workspace, transport)
    else:
        assert _verify(workspace, transport) == expected
    assert called_paths == [ref_path, approval_path]


def test_workflow_approval_stops_at_main_ref_drift(prepared: tuple[Path, dict[str, object]]) -> None:
    workspace, _ = prepared

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/example/source/git/ref/heads/main"
        return httpx.Response(200, json={"ref": CONTEXT.ref, "object": {"sha": "b" * 40, "type": "commit"}})

    with pytest.raises(ApplyRejected):
        _verify(workspace, httpx.MockTransport(respond))


def _no_request(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"Không được gọi HTTP: {request.method}")


@pytest.mark.parametrize(
    ("field", "value"),
    [("plan_sha256", "0" * 64), ("source_repo", "other/source"), ("target_repo", "other/target"), ("run_id", 99)],
)
def test_workflow_approval_rejects_manifest_drift_before_http(
    prepared: tuple[Path, dict[str, object]], field: str, value: object,
) -> None:
    workspace, manifest = prepared
    _write_manifest(workspace, {**manifest, field: value})
    with pytest.raises(ApplyRejected):
        _verify(workspace, httpx.MockTransport(_no_request))


def test_workflow_approval_rejects_plan_hash_and_repo_drift(prepared: tuple[Path, dict[str, object]]) -> None:
    workspace, _ = prepared
    plan = _plan(workspace)
    plan.items[0].title = "Đã sửa"
    (workspace / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    with pytest.raises(ApplyRejected):
        _verify(workspace, httpx.MockTransport(_no_request))

    _plan(workspace, repo="other/target")
    with pytest.raises(ApplyRejected):
        _verify(workspace, httpx.MockTransport(_no_request))


@pytest.mark.parametrize(
    "context",
    [replace(CONTEXT, run_attempt=2), replace(CONTEXT, event_name="pull_request"), replace(CONTEXT, event_sha="b" * 40)],
)
def test_workflow_approval_rejects_untrusted_run_before_http(
    prepared: tuple[Path, dict[str, object]], context: RunContext,
) -> None:
    workspace, _ = prepared
    with pytest.raises(ApplyRejected):
        _verify(workspace, httpx.MockTransport(_no_request), context)
