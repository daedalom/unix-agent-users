#!/usr/bin/env python3
"""Unit tests for bin/provision-agents that run without root.

A fake passwd/group database and a fake `run()` stand in for the system;
everything that touches the real filesystem is redirected into a tempdir.
The tests pin down the bootstrap matrix (group/users missing in every
combination), idempotency, removal, and the marked-block editing.

Run:  python3 -m unittest discover -s tests
"""

import collections
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "bin", "provision-agents")


def load_module():
    spec = importlib.util.spec_from_loader("provision_agents", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = SCRIPT
    with open(SCRIPT) as f:
        exec(compile(f.read(), SCRIPT, "exec"), mod.__dict__)
    return mod


PW = collections.namedtuple("PW", "pw_name pw_uid pw_gid pw_dir")
GR = collections.namedtuple("GR", "gr_name gr_gid gr_mem")


class FakeSystem:
    """In-memory users/groups plus a command interpreter for run()."""

    def __init__(self, root):
        self.root = root
        self.users = {}
        self.groups = {}
        self.commands = []
        self.next_uid = 1000
        self.homes = os.path.join(root, "home")
        os.makedirs(self.homes)

    # --- fake nss ------------------------------------------------------
    def add_user(self, name, gid=None, home=None):
        uid = self.next_uid
        self.next_uid += 1
        if gid is None:
            gid = uid
            self.groups.setdefault(name, GR(name, gid, []))
        home = home or os.path.join(self.homes, name)
        os.makedirs(home, exist_ok=True)
        self.users[name] = PW(name, uid, gid, home)
        return self.users[name]

    def add_group(self, name, gid=None):
        gid = gid if gid is not None else 5000 + len(self.groups)
        self.groups[name] = GR(name, gid, [])

    def getpwnam(self, n):
        if n not in self.users:
            raise KeyError(n)
        return self.users[n]

    def getgrnam(self, n):
        if n not in self.groups:
            raise KeyError(n)
        return self.groups[n]

    def getpwall(self):
        return list(self.users.values())

    # --- fake run() -----------------------------------------------------
    def run(self, cmd, as_user=None, check=True):
        self.commands.append((as_user, list(cmd)))
        R = collections.namedtuple("R", "returncode stdout stderr")
        c = cmd[0]
        if c == "groupadd":
            self.add_group(cmd[-1])
        elif c == "useradd":
            self.add_user(cmd[-1])
        elif c == "usermod":
            g, u = cmd[2], cmd[3]
            self.groups[g].gr_mem.append(u)
        elif c == "gpasswd":
            u, g = cmd[2], cmd[3]
            if u in self.groups[g].gr_mem:
                self.groups[g].gr_mem.remove(u)
        elif c == "git":
            pass
        elif c == "mkdir":
            os.makedirs(cmd[-1], exist_ok=True)
        elif c == "chmod":
            os.chmod(cmd[-1], int(cmd[1], 8))
        elif c == "test":
            ok = os.path.exists(cmd[-1]) and (cmd[1] != "-f" or os.path.isfile(cmd[-1]))
            return R(0 if ok else 1, "", "")
        elif c == "ssh-keygen":
            keyfile = cmd[cmd.index("-f") + 1]
            open(keyfile, "w").close()
            with open(keyfile + ".pub", "w") as f:
                f.write(f"ssh-ed25519 FAKE {as_user}-agent\n")
        elif c == "cat":
            with open(cmd[-1]) as f:
                return R(0, f.read(), "")
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return R(0, "", "")


CONFIG = """
[setup]
shared_group = agents
state_dir = {root}/state
shim_dir = {root}/shims
target_path = {root}/bin:/nonexistent
[humans]
members = alice
[agents]
claude = claude
codex = codex
"""


class ProvisionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.sys = FakeSystem(self.root)
        self.mod = load_module()
        m = self.mod
        self.doas_conf = os.path.join(self.root, "doas.conf")
        os.makedirs(os.path.join(self.root, "bin"))
        for tool in ("claude", "codex"):
            p = os.path.join(self.root, "bin", tool)
            open(p, "w").close()
            os.chmod(p, 0o755)
        devnull = open(os.devnull, "w")
        self.addCleanup(devnull.close)
        patches = [
            mock.patch.object(m.pwd, "getpwnam", self.sys.getpwnam),
            mock.patch.object(m.pwd, "getpwall", self.sys.getpwall),
            mock.patch.object(m.grp, "getgrnam", self.sys.getgrnam),
            mock.patch.object(m, "run", self.sys.run),
            mock.patch.object(m, "DOAS_CONF", self.doas_conf),
            mock.patch.object(m, "check_root_owned_path", lambda p: None),
            mock.patch.object(m.os, "geteuid", lambda: 0),
            mock.patch.object(m.os, "chown", lambda *a, **k: None),
            mock.patch.object(m.subprocess, "run", self._fake_doas_check),
            mock.patch.object(sys, "stdout", devnull),
            mock.patch.object(sys, "stderr", devnull),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = self.write_config()

    def _fake_doas_check(self, cmd, **kw):
        assert cmd[:2] == ["doas", "-C"], cmd
        return collections.namedtuple("R", "returncode stdout stderr")(0, "", "")

    def write_config(self, text=CONFIG):
        path = os.path.join(self.root, "setup.conf")
        with open(path, "w") as f:
            f.write(text.format(root=self.root))
        return self.mod.Setup(path)

    def provision(self, only=None):
        self.mod.cmd_provision(self.cfg, only)

    def members(self):
        return sorted(self.sys.groups["agents"].gr_mem)

    def read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()

    # --- bootstrap matrix --------------------------------------------------

    def test_fresh_system_nothing_exists(self):
        self.sys.add_user("alice")
        self.provision()
        self.assertIn("agents", self.sys.groups)
        self.assertIn("claude", self.sys.users)
        self.assertIn("codex", self.sys.users)
        self.assertEqual(self.members(), ["alice", "claude", "codex"])

    def test_users_exist_group_missing(self):
        for u in ("alice", "claude", "codex"):
            self.sys.add_user(u)
        self.provision()
        self.assertEqual(self.members(), ["alice", "claude", "codex"])

    def test_group_exists_users_missing(self):
        self.sys.add_user("alice")
        self.sys.add_group("agents")
        self.provision()
        self.assertEqual(self.members(), ["alice", "claude", "codex"])

    def test_everything_exists_but_memberships_missing(self):
        self.sys.add_group("agents")
        for u in ("alice", "claude", "codex"):
            self.sys.add_user(u)
        self.provision()
        self.assertEqual(self.members(), ["alice", "claude", "codex"])

    def test_human_without_account_is_skipped(self):
        self.provision()
        self.assertEqual(self.members(), ["claude", "codex"])
        self.assertNotIn("alice", self.read(self.doas_conf))

    def test_provision_single_agent_on_fresh_system(self):
        self.sys.add_user("alice")
        self.provision(only="claude")
        self.assertIn("claude", self.sys.users)
        self.assertNotIn("codex", self.sys.users)       # not requested
        self.assertEqual(self.members(), ["alice", "claude"])
        # doas rules cover all configured agents; harmless for missing ones
        self.assertIn("permit nopass alice as codex", self.read(self.doas_conf))

    # --- idempotency -------------------------------------------------------

    def test_second_run_changes_nothing(self):
        self.sys.add_user("alice")
        self.provision()
        snapshot = {
            "doas": self.read(self.doas_conf),
            "bashrc": self.read(self.sys.users["alice"].pw_dir, ".bashrc"),
            "wrapper": self.read(self.root, "shims", "claude"),
            "state": self.read(self.root, "state", "agents", "claude.json"),
            "members": self.members(),
        }
        self.sys.commands.clear()
        self.provision()
        self.assertEqual(snapshot["doas"], self.read(self.doas_conf))
        self.assertEqual(snapshot["bashrc"], self.read(self.sys.users["alice"].pw_dir, ".bashrc"))
        self.assertEqual(snapshot["wrapper"], self.read(self.root, "shims", "claude"))
        self.assertEqual(snapshot["state"], self.read(self.root, "state", "agents", "claude.json"))
        self.assertEqual(snapshot["members"], self.members())
        mutating = {"useradd", "groupadd", "usermod", "gpasswd", "ssh-keygen"}
        self.assertFalse([c for _, c in self.sys.commands if c[0] in mutating])

    def test_doas_block_preserves_foreign_rules(self):
        self.sys.add_user("alice")
        with open(self.doas_conf, "w") as f:
            f.write("permit persist :wheel\n")
        self.provision()
        text = self.read(self.doas_conf)
        self.assertTrue(text.startswith(self.mod.MARK_BEGIN))
        self.assertIn("permit nopass alice as claude", text)
        self.assertTrue(text.rstrip().endswith("permit persist :wheel"))

    def test_malformed_doas_block_aborts(self):
        self.sys.add_user("alice")
        with open(self.doas_conf, "w") as f:
            f.write(self.mod.MARK_BEGIN + "\n")  # begin without end
        with self.assertRaises(SystemExit):
            self.provision()
        self.assertEqual(self.read(self.doas_conf), self.mod.MARK_BEGIN + "\n")

    # --- removal / convergence ---------------------------------------------

    def test_stale_member_is_revoked(self):
        self.sys.add_user("alice")
        self.sys.add_user("mallory")
        self.sys.add_group("agents")
        self.sys.groups["agents"].gr_mem.append("mallory")
        self.provision()
        self.assertEqual(self.members(), ["alice", "claude", "codex"])

    def test_primary_group_member_is_rejected(self):
        self.sys.add_group("agents", gid=777)
        self.sys.add_user("alice")
        self.sys.add_user("eve", gid=777)
        with self.assertRaises(SystemExit):
            self.provision()

    def test_agent_removed_from_config_is_pruned(self):
        self.sys.add_user("alice")
        self.provision()
        self.assertTrue(os.path.exists(os.path.join(self.root, "shims", "codex")))
        self.cfg = self.write_config(CONFIG.replace("codex = codex\n", ""))
        self.provision()
        self.assertFalse(os.path.exists(os.path.join(self.root, "shims", "codex")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "agents", "codex.json")))
        self.assertEqual(self.members(), ["alice", "claude"])
        self.assertNotIn("as codex", self.read(self.doas_conf))
        self.assertIn("codex", self.sys.users)  # account retained

    def test_modified_wrapper_is_not_deleted_on_removal(self):
        self.sys.add_user("alice")
        self.provision()
        wp = os.path.join(self.root, "shims", "codex")
        with open(wp, "a") as f:
            f.write("# tampered\n")
        self.cfg = self.write_config(CONFIG.replace("codex = codex\n", ""))
        self.provision()
        self.assertTrue(os.path.exists(wp))

    def test_wrapper_is_repaired_when_modified(self):
        self.sys.add_user("alice")
        self.provision()
        wp = os.path.join(self.root, "shims", "claude")
        good = self.read(wp)
        with open(wp, "a") as f:
            f.write("# tampered\n")
        self.provision()
        self.assertEqual(self.read(wp), good)

    # --- config validation -------------------------------------------------

    def test_duplicate_command_rejected(self):
        with self.assertRaises(SystemExit):
            self.write_config(CONFIG.replace("codex = codex", "codex = claude"))

    def test_path_like_command_rejected(self):
        with self.assertRaises(SystemExit):
            self.write_config(CONFIG.replace("codex = codex", "codex = ../codex"))

    def test_quote_in_shim_dir_rejected(self):
        with self.assertRaises(SystemExit):
            self.write_config(CONFIG.replace("shim_dir = {root}/shims", "shim_dir = {root}/sh'ims"))

    # --- human rc files ----------------------------------------------------

    def test_new_bashrc_gets_human_uid_and_gid(self):
        alice = self.sys.add_user("alice")
        chowns = []
        with mock.patch.object(self.mod.os, "chown", lambda p, u, g: chowns.append((p, u, g))):
            self.provision()
        rc = [c for c in chowns if c[0].endswith(".bashrc.agents-setup.tmp")]
        self.assertEqual(rc, [(os.path.join(alice.pw_dir, ".bashrc.agents-setup.tmp"),
                               alice.pw_uid, alice.pw_gid)])

    def test_existing_profile_keeps_mode_and_block_is_prepended(self):
        alice = self.sys.add_user("alice")
        prof = os.path.join(alice.pw_dir, ".profile")
        with open(prof, "w") as f:
            f.write("export FOO=1\n")
        os.chmod(prof, 0o600)
        self.provision()
        text = self.read(prof)
        self.assertTrue(text.startswith(self.mod.MARK_BEGIN))
        self.assertTrue(text.endswith("export FOO=1\n"))
        self.assertEqual(stat.S_IMODE(os.stat(prof).st_mode), 0o600)

    # --- agent-home invariant ----------------------------------------------

    def test_agent_home_is_only_touched_as_agent(self):
        self.sys.add_user("alice")
        self.provision()
        for as_user, cmd in self.sys.commands:
            touches_home = any(self.sys.homes + "/claude" in a for a in cmd)
            if touches_home:
                self.assertEqual(as_user, "claude", cmd)


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def test_insert_into_empty(self):
        out = self.m.apply_block("", ["a"])
        self.assertEqual(out.splitlines(), [self.m.MARK_BEGIN, self.m.BLOCK_COMMENT, "a", self.m.MARK_END])

    def test_replace_existing_keeps_surroundings(self):
        old = "\n".join(["x", self.m.MARK_BEGIN, "junk", self.m.MARK_END, "y", ""])
        out = self.m.apply_block(old, ["b"])
        self.assertEqual(out.splitlines(),
                         ["x", self.m.MARK_BEGIN, self.m.BLOCK_COMMENT, "b", self.m.MARK_END, "y"])

    def test_idempotent(self):
        once = self.m.apply_block("tail\n", ["r1", "r2"])
        self.assertEqual(once, self.m.apply_block(once, ["r1", "r2"]))

    def test_duplicate_block_raises(self):
        text = "\n".join([self.m.MARK_BEGIN, self.m.MARK_END] * 2)
        with self.assertRaises(RuntimeError):
            self.m.parse_block(text)

    def test_out_of_order_raises(self):
        with self.assertRaises(RuntimeError):
            self.m.parse_block(self.m.MARK_END + "\n" + self.m.MARK_BEGIN + "\n")


if __name__ == "__main__":
    unittest.main()
