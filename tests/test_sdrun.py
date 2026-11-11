# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Invariant I-6: no podman command runs in the service's mount namespace.

The point of these tests is not that the current code is correct — it is that
the next person to reintroduce a direct ``podman`` call fails the suite instead
of the fleet. On 2026-11-03 a direct ``podman secret ls`` from the sandboxed
service birthed the rootless pause process inside the service's namespace, and
every container started afterwards failed ``rc=126`` because the kernel
refused its ``/proc`` mount. The unit file looked fine; the invariant has to be
checked where it can actually be enforced.

``test_no_direct_podman_spawn_in_the_service`` is the structural one. The rest
pin the individual call sites that were wrong.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import subprocess
import unittest
from unittest import mock

from agentbridge.service import checkpoint as checkpoint_mod
from agentbridge.service import sdrun
from agentbridge.service import secrets as secrets_mod
from agentbridge.service import tenant as tenant_mod

SERVICE_DIR = pathlib.Path(sdrun.__file__).parent

# ``sdrun`` is the one module that may name a spawn function next to the argv
# it wraps; it spawns ``systemd-run``, never ``podman``, and the check below
# confirms that rather than taking it on trust.
SPAWNERS = {"create_subprocess_exec", "run", "Popen", "call", "check_output",
            "check_call", "create_subprocess_shell"}


def _first_argv_literals(call: ast.Call) -> list[str]:
    """The leading string literals of a spawn call's argv, if they are static.

    Handles both shapes the service uses: ``f(*argv)`` style with the command
    as separate positional arguments, and ``f([...])`` with a list literal.
    """
    if not call.args:
        return []
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        items = first.elts
    else:
        items = call.args
    out = []
    for node in items:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        else:
            break
    return out


class StructuralGuard(unittest.TestCase):
    def test_no_direct_podman_spawn_in_the_service(self):
        """No module spawns a process whose argv starts with ``podman``.

        A podman call must be wrapped by ``sdrun``, which means the literal
        ``"podman"`` may appear in an argv that is *passed* somewhere, but
        never as the command of a spawn call.
        """
        offenders = []
        for path in sorted(SERVICE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else getattr(func, "id", None))
                if name not in SPAWNERS:
                    continue
                # ``sdrun.run([...])`` is the wrapper, not a bypass of it: the
                # argv it is handed is meant to start with "podman".
                if (isinstance(func, ast.Attribute)
                        and getattr(func.value, "id", None) == "sdrun"):
                    continue
                argv = _first_argv_literals(node)
                if argv and argv[0] == "podman":
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "these spawn podman directly; wrap them with sdrun (invariant I-6, "
            f"review log 49): {offenders}")

    def test_sdrun_itself_only_ever_spawns_systemd_run(self):
        """The one module exempted above must earn the exemption."""
        tree = ast.parse((SERVICE_DIR / "sdrun.py").read_text(encoding="utf-8"))
        spawned = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None))
            if name in SPAWNERS:
                spawned.append(_first_argv_literals(node))
        # Every spawn in sdrun passes an already-wrapped argv built by wrap(),
        # so none of them carries a literal command at all.
        self.assertEqual([s for s in spawned if s], [])
        self.assertIn('"systemd-run"',
                      (SERVICE_DIR / "sdrun.py").read_text(encoding="utf-8"))

    def test_the_guard_can_actually_fail(self):
        """A direct podman spawn is detected — otherwise the test above is
        decorative, which is the failure mode it exists to prevent."""
        src = 'import subprocess\nsubprocess.run(["podman", "secret", "ls"])\n'
        tree = ast.parse(src)
        found = [_first_argv_literals(n) for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) in SPAWNERS]
        self.assertIn(["podman", "secret", "ls"], found)


class Wrapping(unittest.TestCase):
    def test_wrap_starts_a_transient_user_unit(self):
        argv = sdrun.wrap(["podman", "run", "--rm", "img"], description="d")
        self.assertEqual(argv[:6], ["systemd-run", "--user", "--quiet",
                                    "--collect", "--wait", "--pipe"])
        self.assertEqual(argv[-4:], ["podman", "run", "--rm", "img"])

    def test_wrap_never_names_a_unit(self):
        """A fixed --unit= would collide between two concurrent callers."""
        argv = sdrun.wrap(["podman", "secret", "ls"], description="d")
        self.assertFalse([a for a in argv if a.startswith("--unit")])

    def test_runtime_max_sec_is_a_backstop_and_can_be_dropped(self):
        self.assertIn("--property=RuntimeMaxSec=300",
                      sdrun.wrap(["podman"], description="d"))
        self.assertNotIn(
            "--property=RuntimeMaxSec=300",
            sdrun.wrap(["podman"], description="d", max_seconds=None))


class SecretsPath(unittest.TestCase):
    """The credential path: wrapped, and still never putting the value in argv."""

    def test_default_runner_is_sdrun(self):
        for fn in (secrets_mod.create, secrets_mod.remove, secrets_mod.names):
            self.assertIs(fn.__kwdefaults__["runner"], sdrun.runner,
                          f"{fn.__name__} must default to sdrun.runner")

    def test_runner_wraps_and_keeps_the_value_on_stdin(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            sdrun.runner(["podman", "secret", "create", "n", "-"], b"TOKEN")
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(argv[0], "systemd-run")
        self.assertIn("podman", argv)
        self.assertEqual(kwargs["input"], b"TOKEN")
        self.assertNotIn("TOKEN", " ".join(argv))

    def test_probe_uses_the_callers_runner(self):
        """``_supports_replace`` must not reach for a direct runner: that was
        a podman call escaping the wrapper by the back door."""
        secrets_mod._replace_support = None
        self.addCleanup(setattr, secrets_mod, "_replace_support", None)
        calls = []

        def runner(argv, stdin=None):
            calls.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 0, b"--replace", b"")

        secrets_mod.create("n", "v", runner=runner)
        self.assertIn(["podman", "secret", "create", "--help"], calls)


class LeafAndQueryPaths(unittest.TestCase):
    def test_run_leaf_goes_through_sdrun(self):
        seen = {}

        async def fake_run(argv, *, description, timeout=None, max_seconds=None):
            seen["argv"] = list(argv)
            return 0, b'{"ok": true}', b""

        with mock.patch.object(checkpoint_mod.sdrun, "run", fake_run):
            out = asyncio.run(checkpoint_mod.run_leaf(
                ["podman", "run", "--rm", "img"]))
        self.assertEqual(out, {"ok": True})
        self.assertEqual(seen["argv"], ["podman", "run", "--rm", "img"])

    def test_run_leaf_reports_a_failure_without_raising_os_errors(self):
        async def fake_run(argv, *, description, timeout=None, max_seconds=None):
            return 125, b"", b"Error: no such image"

        with mock.patch.object(checkpoint_mod.sdrun, "run", fake_run):
            with self.assertRaises(checkpoint_mod.CheckpointError) as ctx:
                asyncio.run(checkpoint_mod.run_leaf(["podman", "run", "img"]))
        self.assertIn("no such image", str(ctx.exception))

    def test_podman_secret_names_goes_through_sdrun(self):
        seen = {}

        async def fake_run(argv, *, description, timeout=None, max_seconds=None):
            seen["argv"] = list(argv)
            return 0, b"anthropic-alice-apikey\nanthropic-bob-oauth\n", b""

        with mock.patch.object(tenant_mod.sdrun, "run", fake_run):
            names = asyncio.run(tenant_mod.podman_secret_names())
        self.assertEqual(seen["argv"][:3], ["podman", "secret", "ls"])
        self.assertEqual(names, {"anthropic-alice-apikey", "anthropic-bob-oauth"})


if __name__ == "__main__":
    unittest.main()
