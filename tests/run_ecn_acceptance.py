"""集中运行ECN及概述/审批/权限依赖测试，包含普通函数形式的回归用例。"""

import importlib
import inspect
import sys
import unittest
from pathlib import Path


def build_suite(*, cross_module: bool = False) -> unittest.TestSuite:
    directory = Path(__file__).parent
    names = sorted({
        "tests." + path.stem
        for pattern in ("test_ecn*.py", "test_overview*.py", "test_components*.py")
        for path in directory.glob(pattern)
    })
    names += [
        "tests.test_approval_workflow", "tests.test_db_storage_entities", "tests.test_access_control",
        "tests.test_workflow_notifications", "tests.test_wecom_cards",
        "tests.test_requirement_overview_impact", "tests.test_project_state_transition",
    ]
    if cross_module:
        for pattern in (
            "test_requirement*.py", "test_information*.py", "test_error_management*.py",
            "test_sample_issue_collection.py", "test_sample_order_dashboard.py", "test_project*.py",
            "test_utils*.py", "test_design_knowledge_workflow.py", "test_issue_workflow_utils.py",
        ):
            names.extend("tests." + path.stem for path in directory.glob(pattern))
    names = sorted(set(names))
    suite = unittest.TestSuite()
    for name in names:
        module = importlib.import_module(name)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        if not hasattr(module, "load_tests"):
            for function_name, function in inspect.getmembers(module, inspect.isfunction):
                if function_name.startswith("test_") and function.__module__ == name and not inspect.signature(function).parameters:
                    suite.addTest(unittest.FunctionTestCase(function))
    print(f"MODULES {len(names)} TESTS {suite.countTestCases()}", flush=True)
    return suite


if __name__ == "__main__":
    suite = build_suite(cross_module="--cross-module" in sys.argv)
    if "--list" not in sys.argv:
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        raise SystemExit(not result.wasSuccessful())
