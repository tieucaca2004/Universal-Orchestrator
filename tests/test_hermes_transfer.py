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
    import hashlib, os, shutil, sys
    CONTAINER_ROOT = {container_root!r}
    HASH_SUPPORTED = {hash_supported!r}

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
        if cmd == "sh":
            # Only form this module ever runs: sh -c "command -v sha256sum"
            sys.exit(0 if HASH_SUPPORTED else 1)
        if cmd == "find" and "-exec" in args:
            # find <container-side-path> -type f -exec sha256sum {{}} +
            container_side_root = args[3]
            host_side_root = resolve(container + ":" + container_side_root)
            for dirpath, _, filenames in os.walk(host_side_root):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, host_side_root)
                    digest = hashlib.sha256(open(full, "rb").read()).hexdigest()
                    container_side_full = os.path.join(container_side_root, rel)
                    sys.stdout.write(f"{{digest}}  {{container_side_full}}\\n")
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


def _write_fake_docker(tmp_dir: Path, container_root: Path, *, hash_supported: bool = True) -> str:
    script = _FAKE_DOCKER.format(container_root=str(container_root), hash_supported=hash_supported)
    path = tmp_dir / f"fake_docker_{'hash' if hash_supported else 'nohash'}_{id(container_root)}"
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

        def _manifest_then_vanish(docker_bin, container, path, **kwargs):
            manifest = original_container_manifest(docker_bin, container, path, **kwargs)
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


class ContentHashIntegrityTests(HermesTransferTestCase):
    """Audit Finding 16-E regression tests: size/count alone could
    not distinguish two different files of identical byte length."""

    def test_manifests_distinguish_same_size_different_content(self):
        """The core gap this closes: a size/count-only manifest
        cannot tell `_host_manifest`/`_container_manifest`'s own
        prior output apart for two files of identical byte length but
        different bytes. With hashing, they must differ; the sizes
        alone must still be identical, proving this is a genuine
        same-size case and not an accidental size difference."""
        from universal_orchestrator.execution.hermes_transfer import _host_manifest

        (self.host_worktree / "f.txt").write_text("AAAA\n")
        manifest_a = _host_manifest(str(self.host_worktree), include_hash=True)
        (self.host_worktree / "f.txt").write_text("BBBB\n")
        manifest_b = _host_manifest(str(self.host_worktree), include_hash=True)

        sizes_a = {path: size for path, (size, _digest) in manifest_a.items()}
        sizes_b = {path: size for path, (size, _digest) in manifest_b.items()}
        self.assertEqual(sizes_a, sizes_b)  # same byte length -- a real same-size case
        self.assertNotEqual(manifest_a, manifest_b)  # hashes catch what sizes alone could not

    def test_copy_in_catches_container_side_corruption_introduced_during_the_copy(self):
        """A `docker cp` that silently corrupts a file to the exact
        same byte length during copy-in must still be caught.
        Simulated by corrupting the staged file as a side effect of
        the `cp` subprocess call itself, before `copy_in()`'s own
        completeness check (a real, unpatched `_container_manifest`
        call) runs against what is now actually on disk."""
        (self.host_worktree / "f.txt").write_text("AAAA\n")
        transfer = self._transfer()
        self.assertEqual(len("AAAA\n"), len("BBBB\n"))

        import subprocess

        import universal_orchestrator.execution.hermes_transfer as mod

        real_run = subprocess.run

        def _cp_then_corrupt(args, **kwargs):
            result = real_run(args, **kwargs)
            if args[:2] == [transfer._docker_bin, "cp"] and result.returncode == 0:
                for staged in Path(str(self.container_root)).rglob("f.txt"):
                    staged.write_text("BBBB\n")
            return result

        mod.subprocess.run = _cp_then_corrupt
        try:
            with self.assertRaises(HermesTransferError):
                transfer.copy_in(str(self.host_worktree))
        finally:
            mod.subprocess.run = real_run

    def test_falls_back_to_size_only_when_container_has_no_sha256sum(self):
        """Never fails closed just because an arbitrary Hermes image
        lacks sha256sum -- a real limitation this fix cannot verify
        against every possible image, disclosed and tested here
        rather than assumed."""
        no_hash_docker = _write_fake_docker(self.tmp_path, self.container_root, hash_supported=False)
        transfer = HermesWorkspaceTransfer(container="hermes", docker_bin=no_hash_docker)
        (self.host_worktree / "f.txt").write_text("alpha\n")

        staging_path = transfer.copy_in(str(self.host_worktree))
        transfer.copy_out(str(self.host_worktree))

        self.assertEqual((self.host_worktree / "f.txt").read_text(), "alpha\n")
        self.assertFalse(Path(str(self.container_root) + staging_path).exists())


if __name__ == "__main__":
    unittest.main()
