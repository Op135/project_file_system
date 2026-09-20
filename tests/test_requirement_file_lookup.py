import tempfile
import unittest
from pathlib import Path

from src.utils import find_files_with_prefix_and_version


class RequirementFileLookupTests(unittest.TestCase):
    def test_matches_complete_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            expected_file = "RFFM-1519-S_需求配置_V1.0.json"
            for filename in (
                expected_file,
                "RFFM-1519-SH_需求配置_V1.0.json",
                "RFFM-1519-SH_需求配置_V2.0.json",
                "copy_RFFM-1519-S_需求配置_V3.0.json",
            ):
                (directory / filename).write_text("{}", encoding="utf-8")

            result = find_files_with_prefix_and_version(temp_dir, "RFFM-1519-S")

        self.assertEqual(
            result,
            {
                "1.0": {
                    "name": expected_file,
                    "v_a": "1",
                    "v_b": "0",
                }
            },
        )

    def test_keeps_all_exact_project_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for filename in (
                "RFFM-1519-SH_需求配置_V1.0.json",
                "RFFM-1519-SH_需求配置_V2.0.json",
            ):
                (directory / filename).write_text("{}", encoding="utf-8")

            result = find_files_with_prefix_and_version(temp_dir, "RFFM-1519-SH")

        self.assertEqual(
            {version: info["name"] for version, info in result.items()},
            {
                "1.0": "RFFM-1519-SH_需求配置_V1.0.json",
                "2.0": "RFFM-1519-SH_需求配置_V2.0.json",
            },
        )


if __name__ == "__main__":
    unittest.main()
