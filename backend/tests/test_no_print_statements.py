import re
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
PRINT_PATTERN = re.compile(r"\bprint\s*\(")


class NoPrintStatementsTests(unittest.TestCase):
    def test_runtime_code_has_no_print_statements(self):
        targets = [BACKEND_DIR / "app", BACKEND_DIR / "main.py"]
        offenders: list[str] = []

        for target in targets:
            files = [target] if target.is_file() else list(target.rglob("*.py"))
            for file_path in files:
                if "__pycache__" in file_path.parts:
                    continue
                content = file_path.read_text(encoding="utf-8")
                if PRINT_PATTERN.search(content):
                    offenders.append(str(file_path.relative_to(BACKEND_DIR)))

        self.assertEqual([], offenders, msg=f"Found print() statements: {offenders}")


if __name__ == "__main__":
    unittest.main()
