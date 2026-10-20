"""ECN 通知使用的详情页链接。"""

from urllib.parse import urlencode


def build_ecn_detail_path(ecn_id: object) -> str:
    """生成站内 ECN 详情路径；缺少单号时安全回退到列表。"""
    normalized = str(ecn_id or "").strip()
    if not normalized:
        return "/ecn_management"
    return f"/ecn_management?{urlencode({'ecn_id': normalized})}"


def build_ecn_detail_url(base_url: object, ecn_id: object) -> str:
    """生成企业微信可访问的 ECN 详情绝对地址。"""
    normalized_base = str(base_url or "").strip().rstrip("/")
    if not normalized_base:
        return ""
    return f"{normalized_base}{build_ecn_detail_path(ecn_id)}"
