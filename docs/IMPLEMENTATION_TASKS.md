# Backlog dự án github-bot (BOT-001…)

Nguồn: `docs/PLAN_GitHub_Automation_Bot_Claude.md` §7. Chưa gắn assignee thật.

| ID | Epic | Công việc | Ưu tiên | Deliverable | Phụ thuộc |
|---|---|---|---|---|---|
| BOT-001 | Foundation | Khởi tạo Python package + CLI | P0 | `pyproject.toml`, CLI help | — |
| BOT-002 | Foundation | Setup pytest/ruff/CI | P0 | Test workflow pass | BOT-001 |
| BOT-003 | Parser | Định nghĩa `TaskDraft` schema | P0 | Models + validation | BOT-001 |
| BOT-004 | Parser | Parse heading TASK | P0 | Parser + tests | BOT-003 |
| BOT-005 | Parser | Parse bảng Markdown tiếng Việt | P0 | Parser + tests | BOT-003 |
| BOT-006 | Identity | Định nghĩa members registry | P0 | `members.yaml` schema | BOT-003 |
| BOT-007 | Identity | Resolve + reject ambiguous names | P0 | Resolver + tests | BOT-006 |
| BOT-008 | GitHub | API client read-only | P0 | Repo/labels/assignees lookup | BOT-001 |
| BOT-009 | Planner | Issue source marker + dedupe | P0 | Planner + tests | BOT-004, BOT-005, BOT-008 |
| BOT-010 | Planner | Preview MD/JSON + plan hash | P0 | Dry-run artifact | BOT-009 |
| BOT-011 | Executor | Apply approved plan | P0 | Issue create + assignee | BOT-007, BOT-010 |
| BOT-012 | QA | Sandbox E2E + rerun idempotency | P0 | Logs + test report | BOT-011 |
| BOT-013 | Sync | Safe updates + conflict detection | P1 | Issue sync | BOT-012 |
| BOT-014 | Dependencies | Dependabot CLI adapter | P1 | Update artifacts | BOT-012 |
| BOT-015 | Pull Requests | Branch/commit/PR manager | P1 | Dependency PR demo | BOT-014 |
| BOT-016 | Governance | CI + policy + CODEOWNERS checks | P1 | PR gate report | BOT-015 |
| BOT-017 | AI Optional | Schema-constrained AI fallback | P2 | Adapter + tests | BOT-012 |
| BOT-018 | Platform | Multi-repo GitHub App | P2 | Installable app | BOT-016 |
