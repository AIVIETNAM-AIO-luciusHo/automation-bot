# GitHub Automation Bot

CLI Python chuyển file Markdown công việc thành GitHub Issues. Mặc định `dry-run`, `write_enabled=false`.
Hiện ở **M0**: chỉ có khung dự án, `--help`/`--version`; chưa có lệnh ghi GitHub.

## Thiết lập

```bash
uv sync
cp .env.example .env            # token chỉ qua biến môi trường, không commit
cp configs/bot.example.yaml configs/bot.yaml
cp configs/members.example.yaml configs/members.yaml
```

## Kiểm tra

```bash
uv run ruff check
uv run pytest
uv run github-bot --help
```

Đặc tả đầy đủ: `docs/PLAN_GitHub_Automation_Bot_Claude.md`.
