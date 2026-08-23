# All items below are solved in the Python re-implementation (2026-08).

[x] drop a copy of AGENT.md into the agent's home (~/.codex/, ~/.claude/)
    -> managed block rendered from agent/AGENT.md.template into the tool's
       user-level instruction file; written as the agent; per-agent override.

[x] wrapper: option to invoke shadowed binary directly
    -> `claude --direct` (or AGENT_WRAPPER_DIRECT=1) execs the real binary
       as the current user, bypassing doas.

[x] wrapper: option to invoke shadowed binary in tmux session
    -> `claude --tmux [SESSION]` (or AGENT_WRAPPER_TMUX=SESSION); creates the
       session running the agent via doas, attaches if it already exists.

[x] dependency checker
    -> bin/check-deps reports required/recommended tools and installs missing
       ones with `--install` (dnf/apt-get/zypper/pacman aware, escalates via
       doas or sudo when not root).

[x] unsolved: local installation updates (e.g. Claude Code) overwrite agent wrapper
    -> wrappers now live in a root-owned shim directory
       (/usr/local/lib/agent-shims/bin) prepended to humans' PATH; vendor
       self-updaters cannot write there. The wrapper resolves the real binary
       at RUNTIME by scanning target_path (skipping itself), so vendor updates
       of the underlying binary are picked up automatically. `status` detects
       wrapper tampering / moved targets; `provision --all` repairs.
