# -*- encoding: utf-8 -*-
import copy
from datetime import (
    datetime,
)

from ...ecn_management_config import (
    ECN_SCHEMA_CONFIG,
    ECNState,
)


def get_ecn_template() -> dict:
    """
    生成当前系统最新版本的 ECN 标准数据结构模板。
    """
    return {
        "ecn_id": "",
        "form_no": "RF-FM-280-A4",
        # 基本信息
        "basic_info": {
            "title": "",
            "applicant_dept": "",  # 申请部门
            "applicant": "",  # 申请人
            "apply_date": "",  # 申请日期
            "requirement_date": "",  # 需求日期
            "file_no": "",  # 文件编号
            "nature": ECN_SCHEMA_CONFIG["change_natures"][0],  # 变更性质
            "erp_no": "",  # ERP编号
            "reasons": {r: False for r in ECN_SCHEMA_CONFIG["reasons"]},  # 变更原因，按照常量生成未勾选选项
            "other_reason_desc": "",  # 其它原因，用户填写的信息
            "requirements": [],  # 变更要求
            "reason_desc": "",  # 变更原因说明
            "attachments": [],  # ECR申请附件
        },
        # 变更涉及的项目型号
        "target_projects": [],
        # 评审信息
        "review_info": {
            "expanded_projects_mass": [],  # 扩展的转产后项目型号
            "expanded_projects_non_mass": [],  # 扩展的转产前项目型号
            "impact_change_log": [],  # ECN影响字段级审计：项目增删、影响项勾选/取消
            "impacts": {
                dim: False for dim in ECN_SCHEMA_CONFIG["impact_dimensions"]
            },  # 变更影响维度，按照常量生成未勾选选项
            "involved_docs": {
                doc: False for doc in ECN_SCHEMA_CONFIG["document_types"]
            },  # 涉及的文档资料，按照常量生成未勾选选项
            "other_docs_desc": "",  # 其它文档资料说明，用户填写的信息
            # 涉及的物料类别及对应的变更行动，按照常量生成未勾选选项的嵌套字典结构
            "involved_materials": {
                mat: {act: False for act in ECN_SCHEMA_CONFIG["material_actions"]}
                for mat in ECN_SCHEMA_CONFIG["material_categories"]
            },
        },
        # 方案评审完成时会根据已审批的三类方案生成两阶段执行清单
        "execution_info": {},
        "change_items": [],
        # 评审工作流程
        "workflow": {
            "current_state": ECNState.DRAFT,  # ECN当前流程状态
            "current_phase": "ECR_PHASE",  # 当前流程阶段
            "current_step_index": 0,  # 当前步骤索引
            "approval_round": "",  # 每次提交生成新标识，防止旧页面跨审批轮次操作
            "route_type": "",  # 路由类型
            "ecn_level": "",  # 稳定等级编码；空值表示尚未判定，业务上按一般等级处理
            "ecn_level_decisions": [],  # 等级判定历史，记录时机、操作人和前后等级
            "pending_roles": [],  # 当前节点角色集合；实际待审批角色需排除 step_approvals 已通过项
            "step_approvals": {},  # 当前并行节点各角色的审批结果
            "scheme_participants": {},  # 方案参与者
            "impact_handlers": [],  # 实际维护过ECN影响区的具体人员，用于精准待办提醒
        },
        "approval_log": [],  # 审批日志，记录每一步的审批人、时间、意见等信息
        "timestamp": {},  # 时间戳记录，记录每次重要操作的时间和描述，用于前端 O(1) 轮询刷新机制
    }


def get_dept_from_role(role: str) -> str:
    """
    如果传入的角色名称里，含有指定字符串，返回该字符串对应的该角色的部门名称
    """
    role_to_dept_map = {
        "研发": "研发部",
        "销售": "销售部",
        "工程": "工程部",
        "生产": "生产部",
        "质量": "质量部",
        "采购": "采购部",
        "PMC": "物资部",
    }
    for key, dept in role_to_dept_map.items():
        if key in role:
            return dept
    return "其它部门"


def generate_ecn_id(all_ecns: dict) -> str:
    """
    找到all_ecns里最大的当前日期最大序号，加1生成新的ECN编号
    """
    today_str = datetime.now().strftime("%y%m%d")
    prefix = f"ECN{today_str}"
    max_count = 0
    for ecn_id in all_ecns.keys():
        if ecn_id.startswith(prefix):
            try:
                num = int(ecn_id[len(prefix) :])
                if num > max_count:
                    max_count = num
            except ValueError:
                pass
    return f"{prefix}{str(max_count + 1).zfill(2)}"


def generate_initial_ecn_data(
    applicant: str,
    role: str,
    all_ecns: dict,
    *,
    user_service=None,
) -> dict:
    """
    在模板基础上，初始化运行时强相关的动态ECN数据（如单号、时间、申请人）
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 正式编号仅在首次保存的数据库事务中分配。
    ecn_id = ""
    applicant_dept = ""
    if user_service is not None and getattr(user_service, "storage_mode", "legacy_excel") == "database":
        membership = user_service.get_primary_membership(applicant)
        if isinstance(membership, dict):
            applicant_dept = str(membership.get("org_name") or "").strip()
    # 旧 Excel 模式没有组织架构，继续用原角色关键词推导显示部门。
    if not applicant_dept:
        applicant_dept = get_dept_from_role(role)

    new_data = get_ecn_template()
    new_data["ecn_id"] = ecn_id  # 初始化ECN编号
    new_data["basic_info"]["applicant_dept"] = applicant_dept  # 初始化申请部门
    new_data["basic_info"]["applicant"] = applicant  # 初始化申请人
    new_data["basic_info"]["apply_date"] = now_str  # 初始化申请日期
    new_data["basic_info"]["file_no"] = ecn_id  # 初始化文件编号，与ECN编号一致
    new_data["timestamp"][now_str] = f"由 {applicant} 创建草稿"  # 记录一条日志

    return new_data


def append_ecn_approval_log_once(approval_log: list, entry: dict) -> bool:
    """幂等追加相邻的同一条流程日志，避免首次发起时重复落盘。"""
    if approval_log and approval_log[-1] == entry:
        return False
    approval_log.append(copy.deepcopy(entry))
    return True
