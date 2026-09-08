"""Release checks must never hide game errors behind the Godot shutdown workaround."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.package import run_checked


class ExportFailureTests(unittest.TestCase):
    def test_only_completed_export_with_specific_shutdown_assertion_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            pack = Path(directory) / "game.pck"
            pack.write_bytes(b"validity is checked by the mandatory runtime smoke")
            completed = "[ DONE ] savepack\n"
            assertion = 'ERROR: Parameter "singleton" is null.\n   at: is_cmdline_mode (editor/editor_node.cpp:6619)\n'
            for code, stdout, stderr, artifact, accepted in (
                    (-6, completed, assertion, pack, True),
                    (-6, "", assertion, pack, False),
                    (-6, completed, assertion, None, False),
                    (-6, completed, assertion + "SCRIPT ERROR: Parse Error: broken script\n", pack, False),
                    (-6, completed, assertion + "ERROR: Resource file not found\n", pack, False),
                    (0, completed, "ERROR: Resource file not found\n", pack, False),
                    (3, completed, assertion, pack, False)):
                with self.subTest(code=code, stderr=stderr, artifact=artifact), patch("tools.package.subprocess.run", return_value=
                        subprocess.CompletedProcess(["godot"], code, stdout, stderr)):
                    if accepted:
                        run_checked(["godot"], {}, Path(directory), export_pack=artifact)
                    else:
                        with self.assertRaises(RuntimeError):
                            run_checked(["godot"], {}, Path(directory), export_pack=artifact)


if __name__ == "__main__":
    unittest.main()
