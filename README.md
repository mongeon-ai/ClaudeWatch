# ClaudeWatch

A small always-on-top window that lists everything Claude is running on this PC, with CPU and RAM for each item and a pause and stop button on each.

Open it with **`Claude activity.lnk`**. A copy in the Windows Startup folder opens it at sign-in, or run `pythonw claudewatch.pyw`. It needs `psutil`, installed with `pip install --user psutil`.

## What it shows

- **Sessions**: every Claude Code process (desktop app or terminal), named by the session's title. Claude scheduled tasks show as `Scheduled: <id>`. Expand a session to see its running commands, its MCP servers and its active subagents.
- **Scheduled (Windows tasks)**: the E:\AI Task Scheduler jobs (daily deploys, video, GSC pull…), shown only while they are running. The footer shows the next one due.
- **Left running by ended sessions**: processes a session started that are still running after it ended, such as dev servers or Chromium.
- **App**: the Claude desktop app itself, plus anything it launched, such as a busy terminal tab or a preview server.

## Buttons

- **‖ Pause** freezes every process in that item: 0% CPU, and its RAM stays allocated. **▶** resumes it exactly where it was. Paused items are saved to `paused.json`, so a restart of the monitor can still resume them. Closing the window offers to resume them first.
- **✕ Stop** kills the item's processes after asking you first. For a session this ends its current turn, but the conversation is kept, so you can send it a message to carry on.
- Subagents run inside their session's process, so pause or stop the session to pause or stop a subagent.

## How it works

`psutil` finds the processes. Each session's process carries `CLAUDE_CODE_HOST_SESSION_ID` in its environment, and the monitor reads only that variable. It maps the ID to the desktop app's session file (`%LOCALAPPDATA%\Claude-Data\…\claude-code-sessions\…\local_<id>.json`) to get the title. Subagents come from `~/.claude/projects/*/<session>/subagents/*.meta.json`.

The process list refreshes every 2 s, at about 20 ms per refresh. The scheduled-task list refreshes every 60 s.

## Config

Settings live in `config.json`, which is written on first close:

- `task_patterns`: which Windows tasks count as yours.
- `ignore_tasks`: HouseDashboard is listed here because it runs as SYSTEM, so this window can't measure or pause it.
- `refresh_seconds`, `topmost`, `mini`, and the window position.
