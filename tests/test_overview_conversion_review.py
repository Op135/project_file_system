import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import db_storage
from src.overview_conversion_review import (
    OVERVIEW_CONVERSION_REVIEWS_KEY,
    build_conversion_preview,
    collect_conversion_pending_items,
    conversion_target_id,
    get_actionable_conversion_reviews,
    get_conversion_review_pending_count,
    process_conversion_review_batch,
    process_conversion_review_item,
    reconcile_conversion_reviews,
)


class OverviewConversionReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = {
            "结构": {
                "基础信息": [
                    {
                        "label": "size",
                        "title": "结构尺寸",
                        "processing_type": "text",
                        "permission": {"edit_role": ["研发结构"]},
                    }
                ]
            },
            "软件": {
                "版本信息": [
                    {
                        "label": "software_version",
                        "title": "软件版本",
                        "processing_type": "text",
                        "permission": {"edit_role": ["研发软件"]},
                    }
                ]
            },
        }
        self.overview_data = {
            "size": {
                "size-1": {
                    "content": "100 × 80 mm",
                    "creator": "结构工程师",
                    "select_activ_dic": {"1.0": True, "2.0": None},
                    "timestamp": {
                        "2026-10-15 10:00:00": {
                            "creator": "结构工程师",
                            "reason": "其它概述变动，需重新判断",
                            "select_activ_dic": {"1.0": True, "2.0": None},
                        }
                    },
                },
                "old-version-only": {
                    "content": "历史版本内容",
                    "creator": "结构工程师",
                    "select_activ_dic": {"1.0": None},
                },
            },
            "software_version": {
                "software-1": {
                    "content": "V3.2",
                    "creator": "软件工程师",
                    "select_activ_dic": {"2.0": True},
                }
            },
        }
        self.related_records = {
            "size": {
                "size-1": {
                    "open": {
                        "open_related_user": "结构工程师",
                        "record": {
                            "2026-10-15 11:00:00": {
                                "operate_user": "张三",
                                "operate_type": "add_chip",
                                "operate_chip_content": "外壳材料改为PC",
                            }
                        },
                    }
                }
            }
        }
        self.store: dict[str, object] = {
            "P1_over_data": self.overview_data,
            "P1_over_related_record": self.related_records,
            OVERVIEW_CONVERSION_REVIEWS_KEY: {},
        }
        self.fake_app = SimpleNamespace(
            state=SimpleNamespace(user_service=None),
            storage=SimpleNamespace(
                general={
                    "over_config_data": self.config,
                    "over_config_data_flat": {
                        config["label"]: config
                        for groups in self.config.values()
                        for configs in groups.values()
                        for config in configs
                    },
                    "overview_role": {
                        "P1": {
                            "结构": {"latest_user": "最近：结构工程师"},
                            "软件": {"latest_user": "最近：软件工程师"},
                        }
                    },
                    "project_req_max_ver": {"P1": "2.0"},
                    "project_summary": {"P1": {"state": "转产"}},
                }
            ),
        )

    def fake_get_item(self, key, default=None):
        return copy.deepcopy(self.store.get(key, default))

    async def fake_atomic(self, path, updater, *args, **kwargs):
        key = path[0]
        current = copy.deepcopy(self.store.get(key))
        if len(path) == 1:
            result = updater(current, *args, **kwargs)
            if result is not db_storage.ATOMIC_NO_UPDATE:
                self.store[key] = result
            return True
        assert isinstance(current, dict)
        parent = current
        for part in path[1:-1]:
            parent = parent.setdefault(part, {})
        leaf = path[-1]
        result = updater(copy.deepcopy(parent.get(leaf)), *args, **kwargs)
        if result is not db_storage.ATOMIC_NO_UPDATE:
            parent[leaf] = result
            self.store[key] = current
        return True

    def module_patches(self):
        return (
            patch("src.overview_conversion_review.app", self.fake_app),
            patch("src.overview_conversion_review.db_storage.get_item", new=self.fake_get_item),
            patch("src.overview_conversion_review.db_storage.atomic_deep_update", new=self.fake_atomic),
            patch("src.overview_conversion_review.can_edit_overview_item", return_value=True),
        )

    async def test_collects_current_version_and_combines_related_reasons(self) -> None:
        origins = {conversion_target_id("size", "size-1", "2.0"): "preexisting"}
        patches = self.module_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            grouped = collect_conversion_pending_items("P1", "2.0", target_origins=origins)

        self.assertEqual(list(grouped), ["结构"])
        self.assertEqual(len(grouped["结构"]), 1)
        item = grouped["结构"][0]
        self.assertEqual(item.origin, "preexisting")
        self.assertTrue(item.has_special_reason)
        self.assertEqual([reason.category for reason in item.reasons], ["manual", "related"])
        self.assertIn("外壳材料改为PC", item.reasons[1].text)

    async def test_unrecorded_project_is_hidden_from_conversion_todos(self) -> None:
        self.store[OVERVIEW_CONVERSION_REVIEWS_KEY] = {
            "review-1": {
                "id": "review-1",
                "project": "P1",
                "role": "结构",
                "version": "2.0",
                "assignee": "结构工程师",
                "status": "pending",
            }
        }
        patches = self.module_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            self.assertEqual(get_conversion_review_pending_count("结构工程师"), 1)
            self.fake_app.storage.general["project_req_max_ver"].clear()
            self.assertEqual(get_actionable_conversion_reviews("结构工程师"), [])
            self.assertEqual(get_conversion_review_pending_count("结构工程师"), 0)

    async def test_preview_counts_active_and_preexisting_pending_by_role(self) -> None:
        patches = self.module_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            previews = build_conversion_preview("P1", "2.0")

        preview_by_role = {preview.role: preview for preview in previews}
        self.assertEqual(preview_by_role["结构"].conversion_count, 0)
        self.assertEqual(preview_by_role["结构"].existing_pending_count, 1)
        self.assertEqual(preview_by_role["结构"].assignee, "结构工程师")
        self.assertEqual(preview_by_role["软件"].conversion_count, 1)
        self.assertEqual(preview_by_role["软件"].existing_pending_count, 0)

    async def test_reconcile_completes_from_real_state_and_creates_new_round_later(self) -> None:
        origin_id = conversion_target_id("size", "size-1", "2.0")
        patches = self.module_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            created = await reconcile_conversion_reviews(
                "P1",
                initial_origins={origin_id: "preexisting"},
                transition_at="2026-10-16 09:00:00",
                actor="状态切换人",
            )
            self.assertTrue(created)
            reviews = self.store[OVERVIEW_CONVERSION_REVIEWS_KEY]
            assert isinstance(reviews, dict)
            self.assertEqual(len(reviews), 1)
            first_review = next(iter(reviews.values()))
            self.assertEqual(first_review["status"], "pending")
            self.assertEqual(first_review["known_targets"][origin_id]["origin"], "preexisting")

            self.overview_data["size"]["size-1"]["select_activ_dic"]["2.0"] = True
            await reconcile_conversion_reviews("P1")
            reviews = self.store[OVERVIEW_CONVERSION_REVIEWS_KEY]
            assert isinstance(reviews, dict)
            first_review = next(iter(reviews.values()))
            self.assertEqual(first_review["status"], "completed")

            self.overview_data["size"]["size-1"]["select_activ_dic"]["2.0"] = None
            await reconcile_conversion_reviews("P1")
            reviews = self.store[OVERVIEW_CONVERSION_REVIEWS_KEY]
            assert isinstance(reviews, dict)
            self.assertEqual(len(reviews), 2)
            open_reviews = [review for review in reviews.values() if review["status"] == "pending"]
            self.assertEqual(len(open_reviews), 1)
            self.assertEqual(open_reviews[0]["round"], 2)
            self.assertEqual(open_reviews[0]["known_targets"][origin_id]["origin"], "post_conversion")

    async def test_existing_transfer_project_infers_plain_conversion_reason_as_normal(self) -> None:
        target_id = conversion_target_id("size", "size-1", "2.0")
        self.overview_data["size"]["size-1"]["timestamp"]["2026-10-15 10:00:00"][
            "reason"
        ] = "转产待确认"
        self.store["P1_over_related_record"] = {}
        patches = self.module_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            await reconcile_conversion_reviews("P1")

        reviews = self.store[OVERVIEW_CONVERSION_REVIEWS_KEY]
        assert isinstance(reviews, dict)
        review = next(iter(reviews.values()))
        self.assertEqual(review["known_targets"][target_id]["origin"], "conversion")
        self.assertEqual(review["special_count"], 0)

    async def test_batch_keeps_exceptions_and_direct_item_processing_completes_task(self) -> None:
        normal_id = conversion_target_id("size", "size-2", "2.0")
        exception_id = conversion_target_id("size", "size-1", "2.0")
        self.overview_data["size"]["size-2"] = {
            "content": "120 × 90 mm",
            "creator": "结构工程师",
            "select_activ_dic": {"2.0": None},
            "timestamp": {
                "2026-10-16 09:00:00": {
                    "creator": "结构工程师",
                    "reason": "转产待确认",
                    "select_activ_dic": {"2.0": None},
                }
            },
        }
        patches = self.module_patches()

        async def update_state(project, label, chip_id, version, target_state, *_args, **_kwargs):
            self.overview_data[label][chip_id]["select_activ_dic"][version] = target_state
            return True, "状态已修改", copy.deepcopy(self.overview_data[label][chip_id])

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch("src.overview_conversion_review._validate_item_activation", new=AsyncMock(return_value=(True, ""))),
            patch("src.overview_conversion_review.update_overview_chip_state", new=update_state),
            patch("src.overview_conversion_review._publish_changed_labels", new=AsyncMock()),
        ):
            await reconcile_conversion_reviews(
                "P1",
                initial_origins={exception_id: "preexisting", normal_id: "conversion"},
            )
            reviews = self.store[OVERVIEW_CONVERSION_REVIEWS_KEY]
            assert isinstance(reviews, dict)
            review_id = next(iter(reviews))

            batch_result = await process_conversion_review_batch(
                review_id,
                actor="结构工程师",
                exception_target_ids={exception_id},
            )
            self.assertTrue(batch_result.ok)
            self.assertEqual(batch_result.changed_count, 1)
            self.assertEqual(batch_result.remaining_count, 1)
            self.assertTrue(self.overview_data["size"]["size-2"]["select_activ_dic"]["2.0"])
            reviews = self.store[OVERVIEW_CONVERSION_REVIEWS_KEY]
            assert isinstance(reviews, dict)
            self.assertEqual(reviews[review_id]["status"], "exceptions")

            item_result = await process_conversion_review_item(
                review_id,
                exception_id,
                actor="结构工程师",
                target_state=False,
            )
            self.assertTrue(item_result.ok)
            reviews = self.store[OVERVIEW_CONVERSION_REVIEWS_KEY]
            assert isinstance(reviews, dict)
            self.assertEqual(reviews[review_id]["status"], "completed")
            self.assertEqual(item_result.remaining_count, 0)


if __name__ == "__main__":
    unittest.main()
