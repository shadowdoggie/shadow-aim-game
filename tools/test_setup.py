"""Verify native Codex installation integrity and recovery without network or auth."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import setup


class CodexPackageTests(unittest.TestCase):
    def archive(self, name="bin/codex", link=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.mode = 0o755
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = "/outside"
                archive.addfile(member)
            else:
                member.size = 4
                archive.addfile(member, io.BytesIO(b"fake"))
        return output.getvalue()

    def install(self, data, contents, checksum=None):
        checksum = checksum or hashlib.sha256(contents).hexdigest()
        with patch.dict(setup.CODEX_PACKAGES, {"test": ("package.tar.gz", checksum, "codex")}), \
                patch.object(setup, "download", side_effect=lambda url, destination, limit: destination.write_bytes(contents)), \
                patch.object(setup, "run") as run:
            setup.install_codex_package(data, "test")
            return run

    def test_integrity_failure_never_publishes_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                self.install(data, self.archive(), "0" * 64)
            self.assertFalse((data / "runtime/bin/codex").exists())
            self.assertEqual(list((data / "runtime").iterdir()), [])

    def test_paths_and_links_cannot_escape_app_runtime(self):
        for name, link in (("../escaped", False), ("bin/link", True), ("/absolute", False), ("auth.json", False)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                with self.assertRaisesRegex(RuntimeError, "Unexpected path"):
                    self.install(data, self.archive(name, link))
                self.assertFalse((data / "runtime/bin/codex").exists())

    def test_success_checks_app_server_and_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "auth.json").write_text("untouched")
            run = self.install(data, self.archive())
            self.assertEqual((data / "runtime/bin/codex").read_bytes(), b"fake")
            self.assertEqual((data / "auth.json").read_text(), "untouched")
            self.assertEqual(run.call_args_list[1].args[0][1:], ["app-server", "--help"])
            self.assertNotEqual(run.call_args.kwargs["env"]["CODEX_HOME"], str(data))

    def test_existing_codex_is_left_unchanged(self):
        with patch.object(setup.shutil, "which", return_value="/existing/codex"), \
                patch.object(setup, "install_codex_package") as install:
            setup.install_codex(Path("unused"))
            install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
