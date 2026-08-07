import json

from src.ecn_management_config import (
    ECN_CONFIG_PATH,
    ECNState,
    build_overview_validation_signature,
    get_active_overview_row_contents,
    get_ecn_dashboard_pending_count,
    is_ecn_pending_for_user,
    is_ecn_review_info_blank,
    is_ecn_scheme_ready_for_review,
    load_ecn_config,
    register_ecn_impact_handler,
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


def test_handlers_remain_pending_during_scheme_review_and_stop_after_approval():
    reviewing = _ecn_record(
        state=ECNState.ECN_REVIEWING,
        impact_handlers=["工程师A"],
        impact_selected=True,
    )
    executing = _ecn_record(
        state=ECNState.ECN_EXECUTING,
        impact_handlers=["工程师A"],
        impact_selected=True,
    )

    assert is_ecn_pending_for_user(reviewing, "工程师A", "研发硬件") is True
    assert is_ecn_pending_for_user(executing, "工程师A", "研发硬件") is False


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


def test_old_nonblank_record_uses_scheme_participants_as_handler_fallback():
    record = _ecn_record(
        impact_selected=True,
        participants={"历史工程师": "confirmed"},
    )

    assert is_ecn_pending_for_user(record, "历史工程师", "研发硬件") is True
    assert is_ecn_pending_for_user(record, "助理A", "研发助理") is False


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
