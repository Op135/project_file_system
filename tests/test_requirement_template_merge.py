import unittest

from src.utils import merge_data_with_template


def _node(
    node_id: str,
    *,
    input_num_accor: str = "",
    input_name_accor: str = "",
    user_must_out: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "answer_type": "单行文本",
        "input_num_accor": input_num_accor,
        "input_name_accor": input_name_accor,
        "input_tolerance": "",
        "user_must_out": user_must_out or {},
        "option_tolerance_out": {},
        "ref_out": [],
        "options": [],
    }


class RequirementTemplateMergeTests(unittest.TestCase):
    def test_inserted_node_keeps_answer_when_dependency_node_id_is_unchanged(self) -> None:
        old_data = {
            "data": {
                "1": _node("10"),
                "2": _node(
                    "20",
                    input_num_accor="1",
                    input_name_accor="1",
                    user_must_out={"405nm": "12", "810nm": "18"},
                ),
            }
        }
        new_template = {
            "data": {
                "1": _node("222"),
                "2": _node("10"),
                "3": _node("20", input_num_accor="2", input_name_accor="2"),
            }
        }

        merged = merge_data_with_template(old_data, new_template)
        migrated_item = merged["data"]["3"]

        self.assertEqual(migrated_item["user_must_out"], {"405nm": "12", "810nm": "18"})
        self.assertNotIn("ref_old_data", migrated_item)

    def test_changed_dependency_node_id_still_requires_reentry(self) -> None:
        old_data = {
            "data": {
                "1": _node("10"),
                "2": _node("20", input_num_accor="1", user_must_out={"405nm": "12"}),
            }
        }
        new_template = {
            "data": {
                "1": _node("10"),
                "2": _node("30"),
                "3": _node("20", input_num_accor="2"),
            }
        }

        merged = merge_data_with_template(old_data, new_template)
        migrated_item = merged["data"]["3"]

        self.assertEqual(migrated_item["user_must_out"], {})
        self.assertEqual(
            migrated_item["ref_old_data"],
            {
                "main": {"405nm": "12"},
                "tolerance": {},
                "ref": [],
                "reason": "配置结构变更，请核对后重新录入",
            },
        )


if __name__ == "__main__":
    unittest.main()
