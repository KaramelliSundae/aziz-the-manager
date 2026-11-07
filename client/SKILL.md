---
name: offload
description: Run heavy work on the owner's home server instead of this Mac, to save its battery and keep it responsive. Use in projects with offload turned on (there is a note in CLAUDE.local.md) before running builds, big test suites, data processing or anything GPU-heavy. Also handles "/offload on" and "/offload off".
---

# Offload

Heavy work goes to the owner's home server. **Aziz**, the manager there, decides when it runs. You say what it needs.

## Turning it on and off

`/offload on` or `/offload off` in a project: run `~/.claude/skills/offload/scripts/offload.py on` (or `off`) from that project and report the result. It is off everywhere until turned on, and the tools refuse in a project where it is off. Do not turn it on without being asked.

## What goes where

| Work | Where |
| --- | --- |
| Edits, searches, small and quick tests | This Mac |
| Builds, big test suites, data processing | Server, CPU |
| Work that truly needs the GPU | Server, `gpu: true` |
| Something that must stay up, such as an API | Server, `server: true` |

- Before a GPU job, tell the owner in one line that Kamil's local model will be offline while it runs.
- A job without `gpu: true` cannot see the GPU, so a wrong guess fails clearly. Don't ask for the GPU just in case: keeping Kamil available is the owner's main goal.

## Tell Aziz what comes next

Every GPU job says how likely **more GPU work is in the next 15-20 minutes**, as `next_gpu`:
- `unlikely` (the default): Kamil reloads as soon as the job ends. Use this unless you already know another GPU job follows.
- `likely`: the GPU waits up to 20 minutes so Kamil doesn't reload twice. Use this in a series of GPU runs.

When plans change or GPU work is done, send `offload_forecast` with `unlikely` so Kamil returns at once.

## Servers

A job ends by itself. A server (`server: true`) stays up until `offload_cancel`, or until the owner stops it, with no time limit. Nothing else stops it. A GPU server keeps Kamil offline for all that time, so tell the owner in one line first, and cancel it as soon as it is no longer needed.

## Running

Use the MCP tools (`offload_run`, `offload_job`, `offload_wait`, `offload_logs`, `offload_pull`, `offload_cancel`, `offload_server`, `offload_files`), or the same operations through `~/.claude/skills/offload/scripts/offload.py` if the tools aren't there.

1. `offload_run` copies the project and returns a job name at once. It doesn't wait for the job.
2. `offload_wait` or `offload_job`, then `offload_logs`.
3. `offload_pull` brings results back. The server's copy keeps `node_modules` and other caches between runs.

## What to expect

- **Jobs can be interrupted.** The image app comes first, and the owner can put Kamil first from the app. An interrupted job restarts from the beginning, at most 3 times; read `note`. Send `restart: false` when rerunning isn't safe, such as anything that deploys or sends something.
- **Queued is normal.** `note` says what it waits for, such as a server that holds the GPU. Don't resubmit.
- **Jobs have the internet but not the local network.** They can't reach this machine's services, the home network or the tailnet.
- **Limits:** 8 GB of memory, 8 CPU cores, 4 hours on the GPU or 12 on CPU unless `max_minutes` says otherwise, up to a day.
- **Secrets never leave this Mac,** even tracked ones: `.env`, keys, certificates, `client_secret*`, `credentials*`, anything with `password` or `secret` in its name. If a build needs one, ask the owner rather than renaming it to get past the filter. `offload_files` shows what would be copied.
- **Missing tools:** install into the job's own space (`pip --user`, `npm`, `uv`). Ask the owner before anything system-wide.
