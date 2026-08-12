import json

from src.ecn_management_config import (
    ECN_CONFIG_PATH,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_SCHEME_GROUP_UNKNOWN,
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_ITEM_STATUS_REVISED_CONFIRMED,
    ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECNState,
    build_overview_validation_signature,
    classify_ecn_change_item,
    get_active_overview_row_contents,
    get_ecn_scheme_coverage,
    get_ecn_dashboard_pending_count,
    has_unrevised_rejected_scheme_items,
    is_ecn_pending_for_user,
    is_ecn_review_info_blank,
    is_ecn_scheme_ready_for_review,
    load_ecn_config,
    register_ecn_impact_handler,
    confirm_revised_scheme_items,
    mark_rejected_scheme_item_revised,
    reject_ecn_scheme_items,
)


def _ecn_record(
    *,
    state=ECNState.ECN_SCHEMING,
    impact_handlers=None,
    impact_selected=False,
    participants=None,
    pending_roles=None,
    applicant="申请人",
):
    return {
        "basic_info": {"applicant": applicant},
        "review_info": {
            "expanded_projects_mass": [],
            "expanded_projects_non_mass": [],
            "impacts": {"光学部件": impact_selected},
            "involved_docs": {"光学件图纸": False},
            "involved_materials": {"光源": {"新增": False}},
            "other_docs_desc": "",
        },
        "workflow": {
            "current_state": state,
            "pending_roles": pending_roles or [],
            "impact_handlers": impact_handlers or [],
            "scheme_participants": participants or {},
        },
    }


def test_checked_in_config_file_is_valid():
    with ECN_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        raw_config = json.load(config_file)

    loaded = load_ecn_config(raw_config)

    assert loaded["permissions"]["impact_initial_reminder_roles"] == ["研发助理"]
    assert loaded["reminders"]["impact_followup_states"] == [
        ECNState.ECN_SCHEMING,
        ECNState.ECN_REVIEWING,
    ]
    assert loaded["ui"]["overview_conflict_auto_close_seconds"] == 5.0
    assert loaded["scheme_review"]["require_rejected_item_selection"] is True
    assert loaded["scheme_review"]["require_revision_before_reconfirmation"] is True
    assert loaded["scheme_review"]["participant_statuses"]["confirmed"]["remind"] is False
    assert loaded["scheme_review"]["participant_statuses"]["needs_reconfirmation"]["remind"] is True
    assert loaded["scheme_review"]["transitions"] == {
        "participant_after_edit": "editing",
        "participant_after_confirmation": "confirmed",
        "participant_after_rejection": "needs_reconfirmation",
        "item_after_rejection": "needs_improvement",
        "item_after_revision": "revised_pending_confirmation",
        "item_after_reconfirmation": "revised_confirmed",
    }


def test_empty_impact_only_reminds_rd_assistant():
    record = _ecn_record()

    assert is_ecn_pending_for_user(record, "助理A", "研发助理") is True
    assert is_ecn_pending_for_user(record, "工程师A", "研发硬件") is False
    assert is_ecn_pending_for_user(record, "工程师B", "工程") is False


def test_explicit_impact_handlers_replace_broad_role_reminder():
    record = _ecn_record(
        impact_handlers=["工程师A", "工程师B"],
        impact_selected=True,
    )

    assert is_ecn_pending_for_user(record, "工程师A", "研发硬件") is True
    assert is_ecn_pending_for_user(record, "工程师B", "工程") is True
    assert is_ecn_pending_for_user(record, "助理A", "研发助理") is False
    assert is_ecn_pending_for_user(record, "工程师C", "质量") is False


def test_registering_impact_handlers_supports_multiple_people_and_does_not_duplicate():
    record = _ecn_record(impact_selected=True)

    assert register_ecn_impact_handler(record, "工程师A") is True
    assert register_ecn_impact_handler(record, "工程师A") is False
    assert register_ecn_impact_handler(record, "工程师B") is True
    assert record["workflow"]["impact_handlers"] == ["工程师A", "工程师B"]

    record["review_info"]["impacts"]["光学部件"] = False
    assert register_ecn_impact_handler(record, "工程师C") is False
    assert record["workflow"]["impact_handlers"] == ["工程师A", "工程师B"]


def test_confirmed_handler_is_not_reminded_but_rejected_handler_is_reminded():
    reviewing = _ecn_record(
        state=ECNState.ECN_REVIEWING,
        impact_handlers=["工程师A"],
        impact_selected=True,
        participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED},
    )
    rejected = _ecn_record(
        state=ECNState.ECN_SCHEMING,
        impact_handlers=["工程师A"],
        impact_selected=True,
        participants={"工程师A": ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION},
    )

    assert is_ecn_pending_for_user(reviewing, "工程师A", "研发硬件") is False
    assert is_ecn_pending_for_user(rejected, "工程师A", "研发硬件") is True


def test_scheme_initiator_is_reminded_when_scheme_is_ready_for_review():
    record = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": "confirmed", "工程师B": "confirmed"},
    )
    record["review_info"]["involved_docs"] = {"光学件图纸": True}
    record["review_info"]["involved_materials"] = {"光源": {"新增": True}}
    record["change_items"] = [
        {
            "linked_docs": ["光学件图纸"],
            "linked_materials": ["光源-新增"],
        }
    ]

    assert is_ecn_scheme_ready_for_review(record) is True
    assert is_ecn_pending_for_user(record, "经理A", "研发经理") is True
    assert is_ecn_pending_for_user(record, "管理员", "admin") is True
    assert is_ecn_pending_for_user(record, "未参与工程师", "研发硬件") is False


def test_scheme_initiator_is_not_reminded_until_confirmation_and_coverage_are_complete():
    unconfirmed = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": "editing"},
    )
    missing_coverage = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": "confirmed"},
    )
    missing_coverage["review_info"]["involved_docs"] = {"光学件图纸": True}
    missing_coverage["change_items"] = []

    assert is_ecn_scheme_ready_for_review(unconfirmed) is False
    assert is_ecn_pending_for_user(unconfirmed, "经理A", "研发经理") is False
    assert is_ecn_scheme_ready_for_review(missing_coverage) is False
    assert is_ecn_pending_for_user(missing_coverage, "经理A", "研发经理") is False


def test_every_change_requirement_must_be_linked_by_at_least_one_scheme():
    record = _ecn_record(
        impact_selected=True,
        impact_handlers=["工程师A"],
        participants={"工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED},
    )
    record["basic_info"]["requirements"] = [
        {"idx": 1, "content": "更新电子BOM"},
        {"idx": 2, "content": "同步修改说明书"},
    ]
    record["change_items"] = [{"req_idxs": [1]}]

    coverage = get_ecn_scheme_coverage(record)
    assert coverage["missing_requirements"] == {"2"}
    assert is_ecn_scheme_ready_for_review(record) is False
    assert is_ecn_pending_for_user(record, "经理A", "研发经理") is False

    record["change_items"].append({"req_idxs": ["2"]})
    assert get_ecn_scheme_coverage(record)["missing_requirements"] == set()
    assert is_ecn_scheme_ready_for_review(record) is True


def test_old_nonblank_record_uses_scheme_participants_as_handler_fallback():
    record = _ecn_record(
        impact_selected=True,
        participants={"历史工程师": "confirmed"},
    )

    assert is_ecn_pending_for_user(record, "历史工程师", "研发硬件") is False
    assert is_ecn_pending_for_user(record, "助理A", "研发助理") is False


def test_rejecting_selected_items_only_reopens_their_authors():
    record = _ecn_record(
        state=ECNState.ECN_REVIEWING,
        impact_selected=True,
        impact_handlers=["工程师A", "工程师B"],
        participants={
            "工程师A": ECN_PARTICIPANT_STATUS_CONFIRMED,
            "工程师B": ECN_PARTICIPANT_STATUS_CONFIRMED,
        },
    )
    record["change_items"] = [
        {"item_id": "A1", "author": "工程师A", "review_status": "normal"},
        {"item_id": "B1", "author": "工程师B", "review_status": "normal"},
    ]

    authors = reject_ecn_scheme_items(
        record,
        ["A1"],
        "经理A",
        "研发经理",
        "参数需要修订",
        "2026-08-12 10:00:00",
    )

    assert authors == {"工程师A"}
    assert record["change_items"][0]["review_status"] == ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
    assert record["change_items"][0]["rejection_info"]["note"] == "参数需要修订"
    assert record["change_items"][0]["rejection_history"] == [
        {
            "reviewer": "经理A",
            "reviewer_role": "研发经理",
            "note": "参数需要修订",
            "time": "2026-08-12 10:00:00",
        }
    ]
    assert record["change_items"][1]["review_status"] == "normal"
    assert record["workflow"]["scheme_participants"] == {
        "工程师A": ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
        "工程师B": ECN_PARTICIPANT_STATUS_CONFIRMED,
    }
    assert is_ecn_pending_for_user(record, "工程师A", "研发硬件") is True
    assert is_ecn_pending_for_user(record, "工程师B", "工程") is False


def test_rejected_item_must_be_revised_before_reconfirmation():
    record = _ecn_record(
        participants={"工程师A": ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION}
    )
    item = {
        "item_id": "A1",
        "author": "工程师A",
        "review_status": ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    }
    record["change_items"] = [item]

    assert has_unrevised_rejected_scheme_items(record, "工程师A") is True
    mark_rejected_scheme_item_revised(item)
    assert item["review_status"] == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
    assert has_unrevised_rejected_scheme_items(record, "工程师A") is False

    confirm_revised_scheme_items(record, "工程师A")
    assert item["review_status"] == ECN_ITEM_STATUS_REVISED_CONFIRMED


def test_normal_approval_and_applicant_pending_rules_are_preserved():
    approval_record = _ecn_record(
        state=ECNState.ECR_REVIEWING,
        pending_roles=["研发经理"],
    )
    draft_record = _ecn_record(state=ECNState.DRAFT, applicant="申请人A")

    assert is_ecn_pending_for_user(approval_record, "经理A", "研发经理") is True
    assert is_ecn_pending_for_user(draft_record, "申请人A", "销售") is True


def test_dashboard_count_counts_each_ecn_once():
    all_ecns = {
        "ECN1": _ecn_record(impact_handlers=["工程师A"], impact_selected=True),
        "ECN2": _ecn_record(impact_handlers=["工程师A"], impact_selected=True),
        "ECN3": _ecn_record(impact_handlers=["工程师B"], impact_selected=True),
        "dirty": None,
    }

    assert get_ecn_dashboard_pending_count(all_ecns, "工程师A", "研发硬件") == 2


def test_impact_blank_detection_covers_project_document_material_and_description():
    base_review = _ecn_record()["review_info"]
    assert is_ecn_review_info_blank(base_review) is True

    for field, value in [
        ("expanded_projects_mass", ["P1"]),
        ("other_docs_desc", "补充说明"),
    ]:
        review = {**base_review, field: value}
        assert is_ecn_review_info_blank(review) is False

    document_review = {**base_review, "involved_docs": {"光学件图纸": True}}
    material_review = {**base_review, "involved_materials": {"光源": {"新增": True}}}
    assert is_ecn_review_info_blank(document_review) is False
    assert is_ecn_review_info_blank(material_review) is False


def test_active_overview_row_contents_only_returns_current_cell_active_data():
    raw_data = {
        "active-a": {
            "row_id": "ROW-1",
            "content": "已有参数A",
            "select_activ_dic": {"2.0": True},
        },
        "active-b": {
            "row_id": "ROW-1",
            "content": "已有参数B",
            "select_activ_dic": {"2.0": True},
        },
        "old-version": {
            "row_id": "ROW-1",
            "content": "旧版本内容",
            "select_activ_dic": {"2.0": False, "1.0": True},
        },
        "other-row": {
            "row_id": "ROW-2",
            "content": "其他基准行内容",
            "select_activ_dic": {"2.0": True},
        },
    }

    assert get_active_overview_row_contents(raw_data, "ROW-1", "2.0") == ["已有参数A", "已有参数B"]


def test_overview_validation_signature_changes_with_content_or_context():
    validated = build_overview_validation_signature(
        "search",
        "有效文件.pdf",
        ["P1", "P2"],
        "光学",
        "light_driver",
    )

    assert validated == build_overview_validation_signature(
        "search",
        "  有效文件.pdf  ",
        ["P1", "P2"],
        "光学",
        "light_driver",
    )
    assert validated != build_overview_validation_signature(
        "search",
        "无效文件.pdf",
        ["P1", "P2"],
        "光学",
        "light_driver",
    )
    assert validated != build_overview_validation_signature(
        "search",
        "有效文件.pdf",
        ["P1", "P3"],
        "光学",
        "light_driver",
    )


def test_new_and_legacy_change_items_are_split_into_explicit_document_groups():
    assert classify_ecn_change_item(
        {"type": "overview_update", "scheme_category": "document"}
    ) == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT
    assert classify_ecn_change_item(
        {"type": "overview_update", "scheme_category": ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT}
    ) == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT
    assert classify_ecn_change_item(
        {"type": "text_desc", "scheme_category": "document"}
    ) == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT
    assert classify_ecn_change_item(
        {"type": "text_desc", "scheme_category": ECN_SCHEME_GROUP_ORDINARY_DOCUMENT}
    ) == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT
    assert classify_ecn_change_item(
        {"type": "text_desc", "scheme_category": ECN_SCHEME_GROUP_MATERIAL}
    ) == ECN_SCHEME_GROUP_MATERIAL
    assert classify_ecn_change_item({"type": "legacy_other"}) == ECN_SCHEME_GROUP_UNKNOWN
