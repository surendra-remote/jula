from __future__ import annotations

import unittest

from junitforge.compile_gate import parse_javac_errors, parse_maven_compiler_errors
from junitforge.vendor.javac import parse_errors


class WindowsJavacErrorParsingTest(unittest.TestCase):
    def test_structured_parser_accepts_windows_drive_letter_paths(self) -> None:
        stderr = """C:\\repo\\src\\test\\java\\sample\\GeneratedTest.java:487: error: method setRnldurn in class GiContractHeaderEntity cannot be applied to given types;
        value.setRnldurn(1);
             ^
  required: byte
  found:    int
  reason: argument mismatch; possible lossy conversion from int to byte
C:\\repo\\src\\test\\java\\sample\\GeneratedTest.java:547: error: incompatible types: possible lossy conversion from int to short
"""
        errors = parse_javac_errors(stderr)
        self.assertEqual(2, len(errors))
        self.assertEqual(487, errors[0].line)
        self.assertEqual(14, errors[0].col)
        self.assertEqual(r"C:\repo\src\test\java\sample\GeneratedTest.java", str(errors[0].file))
        self.assertIn("required: byte", errors[0].detail)
        self.assertIn("found: int", errors[0].detail)
        self.assertIn("lossy conversion", errors[0].render())

    def test_compact_parser_accepts_windows_drive_letter_paths(self) -> None:
        stderr = """C:\\repo\\GeneratedTest.java:12: error: incompatible types: possible lossy conversion from int to byte
  required: byte
  found:    int
"""
        errors = parse_errors(stderr)
        self.assertEqual(1, len(errors))
        self.assertIn("required: byte", errors[0])
        self.assertIn("found: int", errors[0])

    def test_maven_parser_keeps_windows_paths_spaces_columns_and_details(self) -> None:
        output = """[ERROR] C:\\work space\\src\\test\\java\\sample\\GeneratedTest.java:[42,17] incompatible types: java.lang.String cannot be converted to int
[ERROR]   required: int
[ERROR]   found:    java.lang.String
"""
        errors = parse_maven_compiler_errors(output)
        self.assertEqual(1, len(errors))
        self.assertEqual(42, errors[0].line)
        self.assertEqual(17, errors[0].col)
        self.assertEqual(
            r"C:\work space\src\test\java\sample\GeneratedTest.java",
            str(errors[0].file),
        )
        self.assertIn("required: int", errors[0].detail)


if __name__ == "__main__":
    unittest.main()
