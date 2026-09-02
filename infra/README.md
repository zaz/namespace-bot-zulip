# Infra: head box, workhorse, and self-healing

Two persistent Namespace devboxes, nothing depends on anyone's laptop:

- **head** (`zulip-head`, S, `--access_mode shared --auto_stop_idle_timeout 0s`):
  runs the bot under `run-forever.sh` (restart-on-exit supervisor) and
  `watch-workhorse.sh` (pings the workhorse every 5 min; wakes it if stopped,
  restarts its watchdog if dead).
- **workhorse** (`fleet-workhorse`, XL): executes agent turns forwarded by
  `fleet-exec` (head) → `fleet-run.sh` (workhorse; base64 argv, so task text
  is never parsed by a shell). Runs `watch-head.sh` (pings the head every
  5 min; wakes it, restarts the bot supervisor and the head's watchdog).

Recreate the head: create the box as above, copy secrets in out-of-band
(`~/shell-bot/.env`, `~/zulip-ai-bot/zuliprc`, `~/.config/ns/*.json`, the
`devbox` binary), then run `bootstrap-head.sh`.

Lesson learned 2026-09-02: a box whose only activity is outbound long-polling
looks idle to the platform and gets auto-stopped; `--auto_stop_idle_timeout`
and `--access_mode` can only be set at create time.
