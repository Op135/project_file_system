# -*- coding: utf-8 -*-
from pathlib import Path
from unittest import TestCase

from src.tools.operand_data import (
    DEFAULT_SOURCE_URL,
    OperandDataError,
    load_operand_database,
    normalize_source_url,
    parse_category_index,
    parse_operand_page,
    save_operand_database,
)


class OperandDataTests(TestCase):
    def test_normalizes_secured_redirect_url(self) -> None:
        secured = (
            "https://ansyshelp.ansys.com/public/account/secured?returnurl="
            "/Views/Secured/Zemax/v252/zh-Hans/OpticStudio_User_Guide/"
            "OpticStudio_Help/topics/Optimization_Operands_by_Category.html"
        )
        self.assertEqual(normalize_source_url(secured), DEFAULT_SOURCE_URL)

    def test_rejects_non_ansys_hosts(self) -> None:
        with self.assertRaises(OperandDataError):
            normalize_source_url("https://example.com/Optimization_Operands_by_Category.html")

    def test_parses_category_links_and_ignores_navigation(self) -> None:
        links = "".join(
            f'<li><a class="xref" href="Category_{index}.html">分类 {index}</a></li>' for index in range(12)
        )
        html = f"""
        <html><body><h1>分类优化操作数</h1><ul>{links}</ul>
        <a class="link" href="Previous.html">上一页</a></body></html>
        """
        title, categories = parse_category_index(html, DEFAULT_SOURCE_URL)
        self.assertEqual(title, "分类优化操作数")
        self.assertEqual(len(categories), 12)
        self.assertTrue(categories[0]["url"].endswith("Category_0.html"))

    def test_parses_operand_description_and_parameters(self) -> None:
        html = """
        <html><body><h1>玻璃数据约束</h1><table>
          <tr><td><strong>名称</strong></td><td><strong>描述</strong></td></tr>
          <tr><td>GCOS</td><td>玻璃成本，由 <strong>Surf</strong> 定义表面。</td></tr>
          <tr><td>GTCE</td><td>热膨胀系数，由 <strong>Surf</strong> 定义。</td></tr>
          <tr><td>PnGT</td><td>废弃操作数。</td></tr>
        </table></body></html>
        """
        category = parse_operand_page(html, "玻璃数据约束", "https://ansyshelp.ansys.com/page.html")
        self.assertEqual(category["name"], "玻璃数据约束")
        self.assertEqual([item["code"] for item in category["operands"]], ["GCOS", "GTCE", "PnGT"])
        self.assertEqual(category["operands"][0]["parameters"], ["Surf"])

    def test_cache_round_trip(self) -> None:
        data = {"categories": [], "operand_count": 0}
        path = Path("tests/.tmp_operand_cache.json")
        try:
            save_operand_database(path, data)
            self.assertEqual(load_operand_database(path), data)
        finally:
            path.unlink(missing_ok=True)
