"""Dependency-free regression tests for the host watchdog's process matcher."""

import ast
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "imap-bridge-ensure.sh"
SOURCE = SCRIPT.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
TREE = ast.parse(SOURCE)
TREE.body = [node for node in TREE.body if isinstance(node, (ast.ImportFrom, ast.FunctionDef))]
NAMESPACE = {}
exec(compile(TREE, str(SCRIPT), "exec"), NAMESPACE)


class BridgeProcessTests(unittest.TestCase):
    def check_processes(self, argv_lists, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "self").mkdir()
            for index, argv in enumerate(argv_lists, 1):
                process = root / str(index)
                process.mkdir()
                if argv is not None:
                    (process / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
            self.assertEqual(NAMESPACE["bridge_running"](root), expected)

    def test_empty(self):
        self.check_processes([], False)

    def test_real_bridge(self):
        self.check_processes([[b"python", b"/data/run_imap_bridge.py"]], True)

    def test_absolute_interpreter(self):
        self.check_processes([[b"/usr/local/bin/python3.12", b"/data/run_imap_bridge.py"]], True)

    def test_old_self_matching_detector_is_not_bridge(self):
        self.check_processes([[b"python", b"-c", b'if b"run_imap_bridge.py" in cmd: pass']], False)

    def test_shell_wrapper_is_not_bridge(self):
        self.check_processes([[b"sh", b"-c", b"python /data/run_imap_bridge.py"]], False)

    def test_other_python_script_argument_is_not_bridge(self):
        self.check_processes([[b"python", b"/tmp/check.py", b"/data/run_imap_bridge.py"]], False)

    def test_similar_name_is_not_bridge(self):
        self.check_processes([[b"python", b"/data/run_imap_bridge.py.bak"]], False)

    def test_disappeared_process_is_ignored(self):
        self.check_processes([None, [b"python", b"-"]], False)


if __name__ == "__main__":
    unittest.main()
