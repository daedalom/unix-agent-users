# agent-setup

Isolation infrastructure for coding agents (Claude Code, Codex, …) on a
single Linux workstation. Agents run as dedicated Unix accounts.

This is a from-scratch re-implementation of the original shell provisioner
in **Python 3 (stdlib only)**. Same security model, same convergence
semantics, plus the previously open TODO items solved (see below).

## Layout

    config/setup.conf              declarative source of truth (INI)
    bin/provision-agents           provision / remove / status (Python)
    bin/check-deps                 dependency checker, optional --install
    wrapper/wrapper-template.sh    doas wrapper template (rendered per agent)
    tests/test_provision.py        root-free unit tests (python3 -m unittest discover -s tests)

Provisioning is idempotent: re-running converges the system toward
`setup.conf` and doubles as the repair/upgrade mechanism.

## Install order

    # 1. edit config/setup.conf (humans, group, agents)
    # 2. check/install prerequisites:
    bin/check-deps                # add --install to install what's missing
    # 3. as root:
    bin/provision-agents provision --all
    # 4. start an agent session by typing its command:
    claude

## Convergence and removal

After changing `HUMANS`, `AGENTS`, or `shared_group`, re-run
`provision-agents provision --all` for full reconciliation.

Root-owned state in `/var/lib/nix-agents` records the managed group and
per-agent artifacts (target binary, wrapper SHA-256, SSH public key).
Provisioning is serialized with an flock so concurrent runs cannot race.
Agents present in state but no longer in the config are revoked automatically;
`provision --agent NAME` converges a single agent; `status` reports drift
(modified wrappers, moved target binaries) without changing anything:

    bin/provision-agents status

Provisioning fully owns supplementary membership in the shared group;
accounts not listed in `humans` or `agents` are removed from it. The group
should therefore not be used to grant unrelated access.

Because doas has no include-directory mechanism, provisioning maintains a
marked block at the start of `/etc/doas.conf` and preserves all other rules.
A candidate file is validated with `doas -C` before it atomically replaces
the original. Rules after the managed block retain doas's last-match
precedence and can override it. Partial, duplicated, or malformed marker
blocks stop provisioning (the original file is never touched in that case).
The same marked-block mechanism is used for human rc files
(`manage_human_path = true`), with one difference: the rc block is *appended*
and unconditionally moves the shim directory to the front of `PATH`. Rc files
commonly add `~/.local/bin` -- where `claude`, `codex` & co. install
themselves -- near the end (RHEL's default `.bashrc` does), and whatever runs
last wins; a prepended block would silently lose to that. The block also
drops empty `PATH` entries (`::`, meaning "current directory") on purpose:
having the cwd in `PATH` is a bad habit that would let an agent-writable
directory shadow commands for the human.

Removing an entry from `agents` revokes its managed group membership and doas
rules and removes its managed wrapper. The Unix account and its home are
deliberately retained. Removing a human similarly revokes managed group and
doas rules:

    bin/provision-agents remove --agent codex
    bin/provision-agents remove --human alice

Group membership is cached in running processes. After removing an account,
terminate its existing sessions; after changing a human's membership, that
human must log out and back in before the kernel view is fully updated.

## Wrappers: direct mode, tmux sessions, self-updater immunity

The generated wrapper shadows the agent command in every human's PATH.
Default invocation runs the real binary as the agent account via doas:

    claude                       # doas -u <agent> -- <real-binary> "$@"

The agent process starts in the caller's current directory (via the agent's
login shell, so its `~/.profile` applies); if the agent account cannot enter
that directory, it starts in the agent's home instead.

Options (also available as environment variables):

    claude --direct              # escape hatch: run the real binary as
                                 # yourself, bypassing doas entirely
    claude --tmux [SESSION]      # run inside tmux session SESSION
                                 # (default: <cmd>-<agent>); attaches if the
                                 # session already exists, so long-running
                                 # agent sessions survive disconnects

    AGENT_WRAPPER_DIRECT=1       # = --direct
    AGENT_WRAPPER_TMUX=name      # = --tmux name
    claude --                    # stop option parsing, pass everything on

**Why wrapper updates can no longer break (the old unsolved problem).**
Previously the wrapper was installed *at* the vendor binary's path, so every
local installation update of e.g. Claude Code overwrote it. Now wrappers
live in a dedicated root-owned **shim directory** (default
`/usr/local/lib/agent-shims/bin`) that is put first in each human's PATH:

1. Only root can write there — vendor self-updaters running as the human
   physically cannot touch the wrappers.
2. The wrapper resolves the real binary at *runtime* by scanning
   `target_path` (skipping the shim dir and itself), falling back to the
   path recorded at provisioning time. When the vendor replaces its own
   binary, the next invocation transparently picks up the new one.
3. `provision-agents status` detects modified wrappers or relocated targets,
   and re-running `provision --all` repairs them.

**The real binary must be installed system-wide.** The wrapper resolves the
binary as the *human* (before switching to the agent), so it must be on
`target_path` and executable by the agent account -- e.g. a root-owned
`/usr/local/bin/claude`. Claude Code's native installer defaults to the
calling user's `~/.local/bin`, which is neither on `target_path` nor readable
by the agent; install it with a system-wide prefix instead (or symlink the
human's copy into `/usr/local/bin` only if the agent can read and execute it).
The upside is that the agent cannot self-update the binary, which is exactly
the tampering class the shim dir exists to prevent. This differs from the
original shell wrapper, which resolved the binary *as the agent* and thus let
each agent account keep its own installation.

**PATH is only prepended for shells that read `~/.bashrc` / `~/.profile`.**
Terminals launched by IDEs or desktop sessions that skip those files will not
see the shim dir; add it to their PATH yourself in that case.

No `AGENT.md`/`CLAUDE.md` files are generated; workflow documentation lives
here in the README only.

## Repository workflow

Provisioning deliberately does not change project ownership, modes, ACLs, or
Git configuration. Human and agent accounts should use independent clones;
do not make one working tree or its `.git` directory writable by both sides.

For repositories hosted on a central service such as GitHub, the recommended
workflow is:

1. Keep the canonical ("blessed") repository protected by the hosting service.
2. Let each agent clone it using its own minimal-scope credentials.
3. Have the agent push to its own branch or fork and open a pull request.
4. Review and merge through the normal human-controlled process.

The SSH keys printed by `provision-agents.sh` should grant access only to the
agent's fork or branches, never administrative access to the blessed repo.

For a fully local dictator-and-lieutenant workflow, keep the blessed repo
human-writable and agent-readable. Give each agent its own clone using Git's
regular transport, which is required for a repo owned by another user and
keeps the object stores independent:

    # as the agent, from an agent-owned directory
    git clone --no-local /srv/git/project.git project

The agent commits to a branch in that clone. The human fetches without
implicitly merging, reviews the result, then integrates selected commits:

    git fetch /home/claude/src/project agent-branch
    git log main..FETCH_HEAD
    git diff main...FETCH_HEAD
    git cherry-pick <commit>       # or merge after review
    git push blessed main

If the human should not have filesystem access to the agent clone, exchange a
`git bundle` or formatted patches through a narrowly scoped handoff directory.
Do not use `git clone --shared`; it makes the agent clone depend on the blessed
repository's object store.

## Security model

| Property                          | Mechanism            | Class       |
|-----------------------------------|----------------------|-------------|
| Agent cannot read protected human files | Unix permissions (checked) | kernel |
| doas delegation                   | doas.conf policy     | root policy |
| Wrapper integrity vs. self-updaters | root-owned shim dir + runtime resolution | filesystem |

## Known hazards

**Human home isolation depends on existing permissions.** Provisioning does
not change ownership, modes, or ACLs on human home directories. It checks
effective access after adding each agent to the shared group and warns if the
agent can list, traverse, or write a configured human's home. Review and
correct the home's ownership, mode, group membership, and ACLs before starting
an untrusted agent session.

**docker group is root-equivalent.** A member of `docker` can run
`docker run -v /:/host …` and own the machine. Provisioning therefore
never adds agents to `docker` and warns if it finds them there. Use
rootless podman for agent container workloads instead:

    bin/check-deps --install          # installs podman if missing
    # per agent, once:  runuser -u claude -- podman info
    # optionally:       alias docker=podman  in the agent's shell rc

**Do not add `keepenv` to doas.conf.** It would leak `SSH_AUTH_SOCK` &
co. into the agent session and undo the credential isolation. Agents get
their own keys (deploy keys with minimal scope), living in their own home.

**Do not share a writable Git working tree across the account boundary.** A
user who can write `.git` metadata can install hooks or executable Git helpers
that later run as another user. Independent clones keep each account's Git
configuration, hooks, index, and object database under that account's control.

## Audit

Everything the agents do is attributable:

    find /path -user claude -newermt '24 hours ago'   # touched files
    git log --author='claude (agent)'                 # commits
