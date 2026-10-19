"""内存PDF预览响应；同一地址允许浏览器重复读取。"""

from starlette.responses import Response

from .config import PDF_PREVIEW_CACHE


def get_cached_pdf_response(cache_id: str) -> Response:
    """返回缓存中的PDF，但不在首次读取时删除，兼容浏览器的重复请求。"""
    pdf_bytes = PDF_PREVIEW_CACHE.get(cache_id)
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        return Response(content="PDF 数据未找到、已过期或会话已结束。", status_code=404)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="document.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
