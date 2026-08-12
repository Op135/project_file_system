import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import db_storage, utils
from src.requirement_overview_impact import (
    REQUIREMENT_OVERVIEW_IMPACT_CONFIG_PATH,
    REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY,
    RequirementOverviewImpactConfigError,
    collect_requirement_change_node_ids,
    load_requirement_overview_impact_config,
    resolve_requirement_overview_impacts,
    save_requirement_overview_impact_config,
)


def test_checked_in_requirement_overview_impact_config_is_valid():
    overview_config_path = REQUIREMENT_OVERVIEW_IMPACT_CONFIG_PATH.parent / "overview_config.json"
    overview_config = json.loads(overview_config_path.read_text(encoding="utf-8"))
    valid_labels = {
        item["label"]
        for role_groups in overview_config.values()
        for group_items in role_groups.values()
        for item in group_items
    }
    loaded = load_requirement_overview_impact_config(valid_overview_labels=valid_labels)
    assert loaded["valid"] is True
    assert loaded["unmapped_policy"] == "all_overviews"


def test_load_and_resolve_selective_impact_config():
    config = load_requirement_overview_impact_config(
        {
            "schema_version": 1,
            "unmapped_policy": "all_overviews",
            "node_impacts": {"23": ["light_source", "light_if"], "75": []},
        },
        valid_overview_labels={"light_source", "light_if", "product_bom"},
    )

    affected, missing = resolve_requirement_overview_impacts(
        {"added": {"23"}, "deleted": {"75"}, "modified": set()},
        config,
        {"light_source", "light_if", "product_bom"},
    )

    assert affected == {"light_source", "light_if"}
    assert missing == set()


def test_unmapped_node_falls_back_to_all_overviews_and_block_policy_rejects_it():
    fallback_config = load_requirement_overview_impact_config(
        {"schema_version": 1, "unmapped_policy": "all_overviews", "node_impacts": {}},
        valid_overview_labels={"a", "b"},
    )
    affected, missing = resolve_requirement_overview_impacts({"modified": {"99"}}, fallback_config, {"a", "b"})
    assert affected == {"a", "b"}
    assert missing == {"99"}

    block_config = load_requirement_overview_impact_config(
        {"schema_version": 1, "unmapped_policy": "block", "node_impacts": {}},
        valid_overview_labels={"a", "b"},
    )
    try:
        resolve_requirement_overview_impacts({"99"}, block_config, {"a", "b"})
    except RequirementOverviewImpactConfigError as exc:
        assert "99" in str(exc)
    else:
        raise AssertionError("block 策略必须拒绝未配置 node_id")


def test_invalid_overview_label_is_rejected():
    try:
        load_requirement_overview_impact_config(
            {
                "schema_version": 1,
                "unmapped_policy": "all_overviews",
                "node_impacts": {"1": ["missing_label"]},
            },
            valid_overview_labels={"known_label"},
        )
    except RequirementOverviewImpactConfigError as exc:
        assert "missing_label" in str(exc)
    else:
        raise AssertionError("不存在的概述 label 必须被配置校验拒绝")


def test_save_config_atomically_updates_file_and_runtime_storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "requirement_overview_impact.json"
        storage = {}
        normalized = save_requirement_overview_impact_config(
            {
                "schema_version": 1,
                "unmapped_policy": "block",
                "node_impacts": {
                    "10": ["product_bom"],
                    "9": ["product_bom", "product_bom"],
                    "2": [],
                    "8": [],
                },
            },
            valid_overview_labels={"product_bom"},
            storage=storage,
            config_path=config_path,
        )

        persisted = json.loads(config_path.read_text(encoding="utf-8"))
        assert persisted == {
            "schema_version": 1,
            "unmapped_policy": "block",
            "node_impacts": {
                "2": [],
                "8": [],
                "9": ["product_bom"],
                "10": ["product_bom"],
            },
        }
        assert list(persisted["node_impacts"]) == ["2", "8", "9", "10"]
        assert list(normalized["node_impacts"]) == ["2", "8", "9", "10"]
        assert "valid" not in persisted
        assert storage[REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY] == normalized
        assert normalized["valid"] is True


def test_save_config_restores_file_and_memory_when_memory_sync_fails():
    class FailOnceStorage(dict):
        def __init__(self, initial_data):
            super().__init__(initial_data)
            self.fail_next_assignment = True

        def __setitem__(self, key, value):
            if key == REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY and self.fail_next_assignment:
                self.fail_next_assignment = False
                raise RuntimeError("模拟内存写入失败")
            super().__setitem__(key, value)

    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "requirement_overview_impact.json"
        original_file = b'{"schema_version": 1, "unmapped_policy": "block", "node_impacts": {}}'
        config_path.write_bytes(original_file)
        original_memory = {
            "schema_version": 1,
            "unmapped_policy": "block",
            "node_impacts": {},
            "valid": True,
            "error": "",
        }
        storage = FailOnceStorage({REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY: original_memory})

        try:
            save_requirement_overview_impact_config(
                {
                    "schema_version": 1,
                    "unmapped_policy": "all_overviews",
                    "node_impacts": {"9": ["product_bom"]},
                },
                valid_overview_labels={"product_bom"},
                storage=storage,
                config_path=config_path,
            )
        except RequirementOverviewImpactConfigError as exc:
            assert "模拟内存写入失败" in str(exc)
        else:
            raise AssertionError("内存同步失败时保存操作必须失败")

        assert config_path.read_bytes() == original_file
        assert storage[REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY] == original_memory


def test_collects_node_ids_from_added_deleted_and_modified_blocks():
    overview_data = {
        "2.0": {
            "added": {"1": {"node_id": "10"}},
            "deleted": {"2": {"node_id": 20}},
            "modified": {"3": {"old_data": {"node_id": "30"}, "new_data": {"node_id": "30"}}},
        }
    }
    assert collect_requirement_change_node_ids(overview_data, "2.0") == {
        "added": {"10"},
        "deleted": {"20"},
        "modified": {"30"},
    }


def test_selective_active_state_marks_only_affected_labels_pending_and_carries_other_states():
    original = {
        "affected": {
            "a": {
                "select_activ_dic": {"1.0": True},
                "enabled": True,
                "icon": None,
                "bg_color": "bg-light-blue-1",
            }
        },
        "unaffected": {
            "b": {
                "select_activ_dic": {"1.0": True},
                "enabled": True,
                "icon": None,
                "bg_color": "bg-light-blue-1",
            }
        },
        "disabled": {
            "c": {
                "select_activ_dic": {"1.0": False},
                "enabled": False,
                "icon": "block",
                "bg_color": "bg-grey-5",
            }
        },
    }
    written = {}
    rollback_context = {}

    async def fake_atomic_update(_path, update_function, *args, **kwargs):
        result = update_function(copy.deepcopy(original), *args, **kwargs)
        if result is not db_storage.ATOMIC_NO_UPDATE:
            written["value"] = result
        return True

    with patch.object(utils.db_storage, "atomic_deep_update", new=fake_atomic_update):
        success, changed_labels = asyncio.run(
            utils.set_overview_active_state(
                "P1",
                "2.0",
                {"affected"},
                rollback_context=rollback_context,
            )
        )

    assert success is True
    assert changed_labels == {"affected", "unaffected", "disabled"}
    assert written["value"]["affected"]["a"]["select_activ_dic"]["2.0"] is None
    assert written["value"]["affected"]["a"]["enabled"] is None
    assert written["value"]["unaffected"]["b"]["select_activ_dic"]["2.0"] is True
    assert written["value"]["unaffected"]["b"]["enabled"] is True
    assert written["value"]["disabled"]["c"]["select_activ_dic"]["2.0"] is False
    assert rollback_context["before"] == original
    assert rollback_context["after"] == written["value"]


def test_overview_state_compensation_uses_compare_and_restore():
    before = {"label": {"chip": {"select_activ_dic": {"1.0": True}}}}
    expected = {"label": {"chip": {"select_activ_dic": {"1.0": True, "2.0": None}}}}
    stored = {"value": copy.deepcopy(expected)}

    async def fake_atomic_update(_path, update_function, *args, **kwargs):
        result = update_function(copy.deepcopy(stored["value"]), *args, **kwargs)
        if result is not db_storage.ATOMIC_NO_UPDATE:
            stored["value"] = result
        return True

    with patch.object(utils.db_storage, "atomic_deep_update", new=fake_atomic_update):
        assert asyncio.run(utils.restore_overview_active_state("P1", before, expected)) is True
        assert stored["value"] == before

        stored["value"] = {"concurrent": "edit"}
        assert asyncio.run(utils.restore_overview_active_state("P1", before, expected)) is False
        assert stored["value"] == {"concurrent": "edit"}


def test_file_snapshot_can_restore_original_or_remove_new_file():
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = Path(temp_dir) / "overview.json"
        file_path.write_bytes(b"old")
        existed, content = utils.snapshot_file_bytes(file_path)
        file_path.write_bytes(b"new")
        utils.restore_file_bytes(file_path, existed, content)
        assert file_path.read_bytes() == b"old"

        new_file_path = Path(temp_dir) / "new-overview.json"
        existed, content = utils.snapshot_file_bytes(new_file_path)
        new_file_path.write_bytes(b"created")
        utils.restore_file_bytes(new_file_path, existed, content)
        assert not new_file_path.exists()


def test_targeted_tidy_excludes_higher_pending_versions():
    async def run_test():
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            req_dir = root / "req"
            over_dir = root / "over"
            req_dir.mkdir()
            over_dir.mkdir()
            for version in ("1.0", "2.0", "3.0"):
                (req_dir / f"P1_需求配置_V{version}.json").write_text("{}", encoding="utf-8")

            fake_app = SimpleNamespace(
                storage=SimpleNamespace(
                    general={
                        "wait_review": {
                            "P1": {
                                "1.0": {"state": "已审"},
                                "2.0": {"state": "待审"},
                                "3.0": {"state": "待审"},
                            }
                        }
                    }
                )
            )

            async def fake_extract(file_dic, file_path):
                version = Path(file_path).stem.rsplit("V", 1)[1]
                node_id = version.split(".", 1)[0]
                item = {"node_id": node_id}
                return {
                    "contrast": {
                        "added": {node_id: item},
                        "deleted": {},
                        "modified": {},
                    },
                    "latest": {"added": {node_id: item}, "file_dic": dict(file_dic)},
                }

            candidate = over_dir / "candidate.json"
            with (
                patch.object(utils, "REQ_DIR", str(req_dir)),
                patch.object(utils, "OVER_DIR", str(over_dir)),
                patch.object(utils, "app", fake_app),
                patch.object(utils, "extract_requirement", new=fake_extract),
            ):
                result_path = await utils.requirement_version_tidy(
                    "P1",
                    False,
                    target_version="2.0",
                    output_path=candidate,
                )

            assert result_path == str(candidate)
            result = json.loads(candidate.read_text(encoding="utf-8"))
            assert result["version"] == "2.0"
            assert "2.0" in result
            assert "3.0" not in result

    asyncio.run(run_test())


def load_tests(_loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for test_function in (
        test_checked_in_requirement_overview_impact_config_is_valid,
        test_load_and_resolve_selective_impact_config,
        test_unmapped_node_falls_back_to_all_overviews_and_block_policy_rejects_it,
        test_invalid_overview_label_is_rejected,
        test_save_config_atomically_updates_file_and_runtime_storage,
        test_save_config_restores_file_and_memory_when_memory_sync_fails,
        test_collects_node_ids_from_added_deleted_and_modified_blocks,
        test_selective_active_state_marks_only_affected_labels_pending_and_carries_other_states,
        test_overview_state_compensation_uses_compare_and_restore,
        test_file_snapshot_can_restore_original_or_remove_new_file,
        test_targeted_tidy_excludes_higher_pending_versions,
    ):
        suite.addTest(unittest.FunctionTestCase(test_function))
    return suite
