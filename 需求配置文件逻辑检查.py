# -*- encoding: utf-8 -*-
import ast
import itertools
import json
import logging
import re
from copy import deepcopy

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class ConfigValidator:
    def __init__(self, json_path):
        self.raw_data = self.load_json(json_path)
        self.data = self.raw_data.get("data", {}) if "data" in self.raw_data else self.raw_data

    def load_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"无法加载JSON文件: {e}")
            return {}

    # -------------------------------------------------------------------------
    # 新增：静态语法与拼写检查 (核心改进部分)
    # -------------------------------------------------------------------------
    def validate_syntax(self, current_node_id, condition_str):
        """
        静态分析 condition 字符串
        特点：
        1. any/all: 必须是列表格式，如 ['A', 'B']
        2. ==/!= : 支持 Python 格式 (123, True) 也支持无引号字符串 (代工)
        """
        if not condition_str or condition_str == "无条件":
            return True, []

        errors = []
        # 1. 切分顶层逻辑
        parts = re.split(r"\s+(?:and|or)\s+", condition_str)

        # 正则提取: ID, 操作符, 值
        pattern = re.compile(r"^\s*(\d+)\s*(==|!=|any|all)\s*(.+)$")

        for part in parts:
            part = part.strip()
            if not part:
                continue

            if part.startswith("not "):
                part = part[4:].strip()

            match = pattern.match(part)
            if not match:
                errors.append(f"表达式格式错误: '{part}'")
                continue

            ref_id, operator, val_str = match.groups()
            val_str = val_str.strip()

            # --- A. 检查引用ID是否存在 ---
            if ref_id not in self.data:
                errors.append(f"引用了不存在的节点 ID: {ref_id}")
                continue

            # --- B. 智能解析值 (核心修改) ---
            target_val = None

            try:
                # 尝试按 Python 标准语法解析 (数字、布尔、列表、带引号的字符串)
                target_val = ast.literal_eval(val_str)
            except (ValueError, SyntaxError):
                # 解析失败了 (例如遇到了 "代工" 这种无引号字符串)

                if operator in ["==", "!="]:
                    # 【豁免逻辑】：如果是等于/不等于，解析失败则视为“原生字符串”
                    # 这样 "代工" 就会被当做字符串 "代工" 处理
                    target_val = val_str
                else:
                    # 【严格逻辑】：如果是 any/all，解析失败通常是因为漏了括号或引号
                    # 例如: any['结构' (漏右括号) 或 any[结构] (漏内部引号)
                    errors.append(f"列表语法错误 '{val_str}': 请确保使用方括号 [] 且内部元素加引号")
                    continue

            # --- C. 检查类型匹配 ---
            if operator in ["any", "all"]:
                if not isinstance(target_val, (list, tuple)):
                    errors.append(f"操作符 '{operator}' 要求列表格式 [...]，但检测到: {val_str}")
                    continue

            # --- D. 检查拼写/选项是否存在 ---
            ref_node_options = self.data[ref_id].get("options", [])

            if ref_node_options:
                valid_outs = {opt["option_out"] for opt in ref_node_options if "option_out" in opt}

                # 统一转成列表处理
                values_to_check = []
                if isinstance(target_val, (list, tuple)):
                    values_to_check = target_val
                else:
                    values_to_check = [target_val]

                for v in values_to_check:
                    # 这里的比对需要转字符串，因为 JSON 里可能是 "12" 而值是 12
                    # 同时也要处理布尔值
                    v_str = str(v)
                    valid_outs_str = [str(vo) for vo in valid_outs]

                    if v_str not in valid_outs_str:
                        # 特殊放行：有些逻辑可能用 True/False 代表有无，即使 option_out 里没写
                        if v_str in ["True", "False"]:
                            continue

                        errors.append(f"无效选项值: '{v}' 不在节点 {ref_id} 的定义中")
                        preview = list(valid_outs)[:3]
                        errors.append(f"    (节点 {ref_id} 合法值示例: {preview}...)")

        return len(errors) == 0, errors

    # -------------------------------------------------------------------------
    # 之前的核心逻辑函数 (逻辑运算)
    # -------------------------------------------------------------------------
    def logic_out(self, k, cond_logic_str, mock_data_snapshot):
        # ... (保持原样，或者直接复制上一版的代码) ...
        # 为了节省篇幅，这里假设 logic_out 和 get_dependent_ids, generate_permutations
        # 与上一版代码完全一致 (修复了正则 bug 的版本)

        # 复制逻辑函数开始
        logic_out_bool = False
        logic_delimiters = ["and", "or"]
        cond_delimiters = ["any", "all", "==", "!="]

        if cond_logic_str == "无条件":
            return True

        logic_pattern = "|".join(f"({re.escape(delimiter)})" for delimiter in logic_delimiters)
        cond_pattern = "|".join(map(re.escape, cond_delimiters))

        logic_result = re.split(logic_pattern, cond_logic_str)
        logic_result = [s for s in logic_result if s]

        elements = [s for s in logic_result if s.strip() not in logic_delimiters]
        separators = [s for s in logic_result if s.strip() in logic_delimiters]

        bool_list = []
        cond_id_list = []

        # 这里的正则提取逻辑保持修复后的版本
        id_pattern = r"(\d+)\s*(?:==|!=|any|all)"
        for p in elements:
            if not p.strip():
                continue
            # 提取依赖ID
            ids = re.findall(id_pattern, p)
            if ids:
                cond_id_list.extend(ids)
            else:
                # 这种在 validate_syntax 会被捕获，这里可以选择跳过或报错
                pass

        cond_id_list = list(set(cond_id_list))

        for c_id in cond_id_list:
            if c_id not in mock_data_snapshot:
                # 这种情况在 validate_syntax 已经处理，这里为了运行安全直接返回False
                return False
            op_user_out = dict(mock_data_snapshot[c_id].get("user_must_out", {}))
            if not op_user_out:
                return False

        for p in elements:
            if not p.strip():
                continue
            cond_result = re.split(cond_pattern, p)

            # 提取当前片段的ID
            match = re.search(r"\d+", cond_result[0])
            if not match:
                continue
            current_cond_id = match.group()

            if current_cond_id not in cond_id_list:
                continue

            op_user_out = dict(mock_data_snapshot[current_cond_id].get("user_must_out", {}))
            op_user_out_list = []
            target_node_options = mock_data_snapshot[current_cond_id].get("options", [])

            if len(op_user_out.keys()) > 0:
                for op_key, op_value in op_user_out.items():
                    if op_value:
                        found = False
                        for op in target_node_options:
                            if op["option_content"] == op_key:
                                op_user_out_list.append(op["option_out"])
                                found = True
                                break
                        if not found and not target_node_options:
                            pass

            try:
                raw_val = cond_result[1].strip()
                try:
                    condition_val = ast.literal_eval(raw_val)
                except Exception:
                    condition_val = raw_val
            except Exception as e:
                # 这种错误现在会在 validate_syntax 中被详细报告
                raise ValueError(f"Value Parse Error: {e}")

            try:
                if "any" in p:
                    # 容错：如果 condition_val 不是 list，尝试转 list
                    c_val = condition_val if isinstance(condition_val, (list, tuple)) else [condition_val]
                    res = any(item in c_val for item in op_user_out_list)
                    bool_list.append(not res if "not" in p else res)
                elif "all" in p:
                    c_val = condition_val if isinstance(condition_val, (list, tuple)) else [condition_val]
                    op_user_set = set(op_user_out_list)
                    cond_set = set(c_val)
                    res = op_user_set.issubset(cond_set)
                    bool_list.append(not res if "not" in p else res)
                elif "==" in p:
                    val = op_user_out_list[0] if op_user_out_list else None
                    bool_list.append(val == condition_val)
                elif "!=" in p:
                    val = op_user_out_list[0] if op_user_out_list else None
                    bool_list.append(val != condition_val)
                else:
                    bool_list.append(False)
            except Exception as e:
                raise RuntimeError(f"Logic Error: {e}")

        result_str = "".join(f"{str(x)} {y} " for x, y in itertools.zip_longest(bool_list, separators, fillvalue=""))

        try:
            allowed_names = {"True": True, "False": False}
            code = compile(result_str, "<string>", "eval")
            logic_out_bool = eval(result_str)
        except Exception:
            return False

        return logic_out_bool
        # 复制逻辑函数结束

    def get_dependent_ids(self, condition_str):
        if not condition_str or condition_str == "无条件":
            return []
        pattern = r"(\d+)\s*(?:==|!=|any|all)"
        return list(set(re.findall(pattern, condition_str)))

    def generate_permutations(self, dependent_ids):
        # ... (保持上一版代码一致) ...
        possibilities = {}
        for nid in dependent_ids:
            if nid not in self.data:
                continue
            node_config = self.data[nid]
            options = node_config.get("options", [])
            answer_type = node_config.get("answer_type", "")
            node_states = []
            node_states.append({})
            if not options:
                node_states.append({"mock_text": "True"})
            else:
                content_list = [opt["option_content"] for opt in options if opt["option_content"]]
                if "单选" in answer_type:
                    for c in content_list:
                        node_states.append({c: True})
                elif "多选" in answer_type:
                    for c in content_list:
                        node_states.append({c: True})
                    all_selected = {c: True for c in content_list}
                    if all_selected:
                        node_states.append(all_selected)
            possibilities[nid] = node_states

        keys = list(possibilities.keys())
        value_lists = [possibilities[k] for k in keys]
        combinations = []
        for combo in itertools.product(*value_lists):
            mock_snapshot = {}
            for i, nid in enumerate(keys):
                mock_snapshot[nid] = {"user_must_out": combo[i], "options": self.data[nid].get("options", [])}
            combinations.append(mock_snapshot)
        return combinations

    # -------------------------------------------------------------------------
    # 主验证流程 (逻辑升级)
    # -------------------------------------------------------------------------
    def run_validation(self):
        print(f"{'=' * 30} 开始详细验证 {'=' * 30}")
        syntax_error_count = 0
        logic_crash_count = 0

        for node_id, node_data in self.data.items():
            condition = node_data.get("condition", "无条件")
            if condition == "无条件":
                continue

            # --- 步骤 1: 静态语法检查 (新增) ---
            # 这步专门用来抓漏括号、漏冒号、拼写错误
            is_valid, syntax_msgs = self.validate_syntax(node_id, condition)
            if not is_valid:
                print(f"❌ [语法/拼写错误] 节点 {node_id}")
                print(f"   Condition: {condition}")
                for msg in syntax_msgs:
                    print(f"   -> {msg}")
                print("-" * 20)
                syntax_error_count += 1
                # 如果语法都错了，后面的逻辑模拟肯定会挂，直接跳过该节点
                continue

            # --- 步骤 2: 逻辑崩溃模拟 (原有逻辑) ---
            dep_ids = self.get_dependent_ids(condition)
            missing_ids = [did for did in dep_ids if did not in self.data]
            if missing_ids:
                continue  # 已经在validate_syntax报过了

            mock_scenarios = self.generate_permutations(dep_ids)
            for mock_data in mock_scenarios:
                try:
                    result = self.logic_out(node_id, condition, mock_data)
                    if not isinstance(result, bool):
                        print(f"❌ [逻辑错误] 节点 {node_id}: 返回非布尔值")
                        logic_crash_count += 1
                        break
                except Exception as e:
                    print(f"❌ [运行时崩溃] 节点 {node_id}")
                    print(f"   Condition: {condition}")
                    print(f"   Error: {e}")
                    logic_crash_count += 1
                    break

        print("\n" + "=" * 30)
        print("验证结果摘要:")
        print(f"1. 语法/拼写错误: {syntax_error_count} 个 (优先修复!)")
        print(f"2. 逻辑/崩溃错误: {logic_crash_count} 个")

        if syntax_error_count == 0 and logic_crash_count == 0:
            print("\n🎉 完美！配置文件无语法错误且逻辑稳定。")


if __name__ == "__main__":
    validator = ConfigValidator("config_service.json")
    validator.run_validation()
