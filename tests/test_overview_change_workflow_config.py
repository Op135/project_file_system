import unittest

from src.overview_change_workflow_config import (
    OverviewChangeWorkflowConfigError,
    load_overview_change_workflow_config,
)


class OverviewChangeWorkflowConfigTests(unittest.TestCase):
    def test_loader_normalizes_roles_states_and_approval_targets(self):
        config = load_overview_change_workflow_config(
            {
                "schema_version": 1,
                "batch_overview": {
                    "tool_roles": ["研发结构", "研发结构", " admin "],
                    "allowed_project_states": ["待定", "研发"],
                    "prevent_self_approval": True,
                    "approval_role_targets": {"研发经理": ["研发结构"]},
                },
                "single_correction": {
                    "prevent_self_approval": False,
                    "approval_role_targets": {"admin": ["研发经理"]},
                },
            }
        )

        self.assertEqual(config["batch_overview"]["tool_roles"], frozenset({"研发结构", "admin"}))
        self.assertEqual(config["batch_overview"]["allowed_project_states"], ("待定", "研发"))
        self.assertEqual(
            config["batch_overview"]["approval_role_targets"],
            {"研发经理": frozenset({"研发结构"})},
        )
        self.assertFalse(config["single_correction"]["prevent_self_approval"])

    def test_empty_permission_lists_can_disable_a_workflow(self):
        config = load_overview_change_workflow_config(
            {
                "schema_version": 1,
                "batch_overview": {
                    "tool_roles": [],
                    "allowed_project_states": [],
                    "prevent_self_approval": True,
                    "approval_role_targets": {},
                },
                "single_correction": {
                    "prevent_self_approval": True,
                    "approval_role_targets": {},
                },
            }
        )

        self.assertEqual(config["batch_overview"]["tool_roles"], frozenset())
        self.assertEqual(config["batch_overview"]["allowed_project_states"], ())
        self.assertEqual(config["single_correction"]["approval_role_targets"], {})

    def test_loader_rejects_invalid_security_configuration(self):
        with self.assertRaisesRegex(OverviewChangeWorkflowConfigError, "prevent_self_approval"):
            load_overview_change_workflow_config(
                {
                    "schema_version": 1,
                    "batch_overview": {
                        "tool_roles": ["研发结构"],
                        "allowed_project_states": ["研发"],
                        "prevent_self_approval": "false",
                        "approval_role_targets": {},
                    },
                    "single_correction": {
                        "prevent_self_approval": True,
                        "approval_role_targets": {},
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
