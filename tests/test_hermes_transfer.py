"""M6 remediation, Item 3 — Hermes copy-in/copy-out data integrity.

Uses a minimal, deterministic fake `docker` executable that performs
the same filesystem operations a real container would (via a real
host directory standing in for "inside the container") — never a
real Hermes container in this automated suite; the one real container
verification is a separate, one-off bounded script, documented
outside the regression test suite (see
docs/EXECUTION_HERMES.md's established pattern for this distinction).
"""

import shutil
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from universal_orchestrator.execution.hermes_transfer import (
    HermesTransferError,
    HermesWorkspaceTransfer,
)

_FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, shutil, sys
    CONTAINER_ROOT = {container_root!r}

    def resolve(spec):
        # "<container>:<path>" -> CONTAINER_ROOT + path
        _, _, path = spec.partition(":")
        return CONTAINER_ROOT + path

    args = sys.argv[1:]
    if args[0] == "exec":
        container, cmd = args[1], args[2]
        if cmd == "mkdir":
            os.makedirs(resolve(container + ":" + args[4]), exist_ok=True)
            sys.exit(0)
        if cmd == "find":
            root = resolve(container + ":" + args[3])
            for dirpath, _, filenames in os.walk(root):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root)
                    size = os.path.getsize(full)
                    sys.stdout.write(f"{{rel}}\\t{{size}}\\n")
            sys.exit(0)
        if cmd == "rm":
            target = resolve(container + ":" + args[4])
            shutil.rmtree(target, ignore_errors=True)
            sys.exit(0)
        sys.exit(1)
    if args[0] == "cp":
        src, dst = args[1], args[2]
        if ":" in src:
            src_dir = resolve(src).rstrip("/.")
            dst_dir = dst
        else:
            src_dir = src.rstrip("/.")
            dst_dir = resolve(dst)
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.isdir(src_dir):
            for name in os.listdir(src_dir):
                s = os.path.join(src_dir, name)
                d = os.path.join(dst_dir, name)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
        sys.exit(0)
    sys.exit(1)
    """
)


def _write_fake_docker(tmp_dir: Path, container_root: Path) -> str:
    script = _FAKE_DOCKER.format(container_root=str(container_root))
    path = tmp_dir / "fake_docker"
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class HermesTransferTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.host_worktree = self.tmp_path / "worktree"
        self.host_worktree.mkdir()
        self.container_root = self.tmp_path / "fake_container_fs"
        self.container_root.mkdir()
        self.docker_bin = _write_fake_docker(self.tmp_path, self.container_root)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _transfer(self) -> HermesWorkspaceTransfer:
        return HermesWorkspaceTransfer(container="hermes", docker_bin=self.docker_bin)


class CopyInTests(HermesTransferTestCase):
    def test_copy_in_verified_complete(self):
        (self.host_worktree / "file_a.txt").write_text("alpha\n")
        transfer = self._transfer()

        staging_path = transfer.copy_in(str(self.host_worktree))

        container_file = Path(str(self.container_root) + staging_path) / "file_a.txt"
        self.assertTrue(container_file.exists())
        self.assertEqual(container_file.read_text(), "alpha\n")

    def test_copy_in_generates_a_unique_adapter_side_staging_path(self):
        (self.host_worktree / "f.txt").write_text("x\n")
        path_a = self._transfer().copy_in(str(self.host_worktree))
        path_b = self._transfer().copy_in(str(self.host_worktree))
        self.assertNotEqual(path_a, path_b)

    def test_second_copy_in_on_same_instance_refused(self):
        (self.host_worktree / "f.txt").write_text("x\n")
        transfer = self._transfer()
        transfer.copy_in(str(self.host_worktree))
        with self.assertRaises(HermesTransferError):
            transfer.copy_in(str(self.host_worktree))


class CopyOutTests(HermesTransferTestCase):
    def test_copy_out_brings_agent_changes_back_to_host(self):
        (self.host_worktree / "f.txt").write_text("original\n")
        transfer = self._transfer()
        staging_path = transfer.copy_in(str(self.host_worktree))

        # Simulate the Agent editing the file inside the container.
        (Path(str(self.container_root) + staging_path) / "f.txt").write_text("edited by agent\n")
        (Path(str(self.container_root) + staging_path) / "new_file.txt").write_text("new\n")

        transfer.copy_out(str(self.host_worktree))

        self.assertEqual((self.host_worktree / "f.txt").read_text(), "edited by agent\n")
        self.assertEqual((self.host_worktree / "new_file.txt").read_text(), "new\n")

    def test_staging_directory_removed_only_after_verified_copy_out(self):
        (self.host_worktree / "f.txt").write_text("x\n")
        transfer = self._transfer()
        staging_path = transfer.copy_in(str(self.host_worktree))
        self.assertTrue(Path(str(self.container_root) + staging_path).exists())

        transfer.copy_out(str(self.host_worktree))

        self.assertFalse(Path(str(self.container_root) + staging_path).exists())

    def test_copy_out_without_copy_in_refused(self):
        with self.assertRaises(HermesTransferError):
            self._transfer().copy_out(str(self.host_worktree))

    def test_copy_out_refuses_when_host_changed_since_copy_in(self):
        (self.host_worktree / "f.txt").write_text("original\n")
        transfer = self._transfer()
        staging_path = transfer.copy_in(str(self.host_worktree))

        # Host Worktree changes unexpectedly between copy_in and copy_out
        # (should not happen under the exclusivity rule -- this is the
        # tripwire, not an assumption).
        (self.host_worktree / "f.txt").write_text("changed on host independently\n")

        with self.assertRaises(HermesTransferError):
            transfer.copy_out(str(self.host_worktree))

        # Refused -- host content is exactly what was just written, never
        # silently overwritten by stale staging content.
        self.assertEqual(
            (self.host_worktree / "f.txt").read_text(), "changed on host independently\n"
        )
        # And the staging directory is preserved, not cleaned up, since
        # the copy-out never happened.
        self.assertTrue(Path(str(self.container_root) + staging_path).exists())

    def test_incomplete_copy_out_preserves_staging_for_recovery(self):
        (self.host_worktree / "f.txt").write_text("x\n")
        transfer = self._transfer()
        staging_path = transfer.copy_in(str(self.host_worktree))

        # Simulate a file silently vanishing from the staging directory
        # right before copy-out is attempted (e.g. a partial write) by
        # adding an extra file to staging that copy-out's own manifest
        # check will catch as a mismatch against what lands on the host.
        # We simulate "incomplete" by removing the host's write
        # permission is not portable; instead corrupt by making the
        # post-copy host manifest diverge: inject an extra file into
        # staging *after* taking the pre-copy-out container manifest
        # would be the real race -- here we directly assert the
        # mismatch path by making the container file disappear between
        # the manifest read and the `cp`, which is exactly what the
        # real failure mode would look like.
        agent_file = Path(str(self.container_root) + staging_path) / "agent_wrote_this.txt"
        agent_file.write_text("in progress\n")

        # Remove it right before copy_out would copy it, simulating a
        # partial/interrupted state discovered by the post-copy manifest
        # check (the file existed when manifested, gone when copied).
        original_container_manifest = __import__(
            "universal_orchestrator.execution.hermes_transfer", fromlist=["_container_manifest"]
        )._container_manifest

        def _manifest_then_vanish(docker_bin, container, path):
            manifest = original_container_manifest(docker_bin, container, path)
            agent_file.unlink()
            return manifest

        import universal_orchestrator.execution.hermes_transfer as mod

        real_fn = mod._container_manifest
        mod._container_manifest = _manifest_then_vanish
        try:
            with self.assertRaises(HermesTransferError):
                transfer.copy_out(str(self.host_worktree))
        finally:
            mod._container_manifest = real_fn

        # Staging directory must still exist -- never cleaned up on an
        # incomplete copy-out.
        self.assertTrue(Path(str(self.container_root) + staging_path).exists())


if __name__ == "__main__":
    unittest.main()
