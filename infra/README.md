# Infra: head box, workhorse, and self-healing

Two persistent Namespace devboxes, nothing depends on anyone's laptop:

- **head** (`zulip-bot-head`, S, created from `head-spec.yaml` with
  `--access_mode shared`): the spec's `sessions` make the platform start
  `run-forever.sh` (bot supervisor) and `watch-workhorse.sh` on EVERY boot,
  so periodic instance restarts (observed every ~3h) self-heal in seconds.
  `watch-workhorse.sh` (pings the workhorse every 5 min; wakes it if stopped,
  restarts its watchdog if dead).
- **workhorse** (`fleet-workhorse`, XL): executes agent turns forwarded by
  `fleet-exec` (head) → `fleet-run.sh` (workhorse; base64 argv, so task text
  is never parsed by a shell). Runs `watch-head.sh` (pings the head every
  5 min; wakes it, restarts the bot supervisor and the head's watchdog).

Recreate the head: `devbox create --from infra/head-spec.yaml --access_mode shared --no_checkout`,
copy secrets in out-of-band
(`~/shell-bot/.env`, `~/zulip-ai-bot/zuliprc`, `~/.config/ns/*.json`, the
`devbox` binary), then run `bootstrap-head.sh`.

Lessons learned 2026-09-02: devbox instances get replaced by the platform
periodically (every ~3h here) and everything running dies — only `sessions`
in the spec survive that. Also: a box whose only activity is outbound long-polling
looks idle to the platform and gets auto-stopped; `--auto_stop_idle_timeout`
and `--access_mode` can only be set at create time.
