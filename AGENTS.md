# Rules for anyone working in this project, people and AI agents

This repository is **public**, with all of its history. Anything pushed here is out for good.

- Never add a person's name or email, a school, a host name, an IP address, a real server path, a user name, a token, a key,
  or the name of or any detail about another project, in code, comments, tests, docs or commit messages. That goes for
  measurements too: say "the real machine".
- What only one machine knows belongs in `/etc/aziz/env` and `/etc/aziz/offload.env` on that machine, never in the repository.
- Commit only as `KaramelliSundae <188345677+KaramelliSundae@users.noreply.github.com>`.
- Before pushing, read the whole diff and the commit message with these rules in mind.
- Run tests on a server under `deploy/capped.sh`: half a core and 1 GB at most, so a test can never hurt what runs there.
