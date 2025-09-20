from pathlib import Path
from typing import Any, Dict

import pandas as pd


class UserService:
    def __init__(self):
        # 获取当前文件的绝对路径
        base_dir = Path(__file__).parent.parent  # 定位到项目根目录
        self.excel_path = base_dir / "data" / "users.xlsx"  # 组合完整路径
        self._lock = False

    # 静态方法。静态方法与类无关，不依赖于类或实例的属性，因此在调用时不需要传递 self 或 cls 参数
    @staticmethod
    def _safe_str_convert(value: Any) -> str:
        """类型安全的字符串转换"""
        if pd.isna(value) or value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _format_password(raw_value: str) -> str:
        """统一密码格式处理"""
        s = raw_value.strip()

        # 处理科学计数法
        if "e" in s.lower():
            try:
                num = float(s)
                return f"{int(num)}" if num.is_integer() else f"{num}"
            except:
                return s

        # 处理浮点尾数
        if "." in s:
            left, right = s.split(".", 1)
            if right == "0" or set(right) == {"0"}:
                return left

        return s

    # 获取对应用户的密码与角色组成的字典
    def get_user(self, username: str) -> dict:
        users = self.load_users()
        user_info = users.get(username, {})
        # 转换 pandas NaN 为 None
        # {"password": "xxx", "role": "user"}
        return {k: v if pd.notna(v) else None for k, v in user_info.items()}

    # 加载包含用户详情的数据
    def load_users(self) -> Dict[str, dict]:
        try:
            df = pd.read_excel(
                self.excel_path,
                engine="openpyxl",
                dtype={"用户名": "string", "密码": "string", "角色": "string"},  # 指定列的数据类型
                # converters={"密码": lambda x: self._safe_str_convert(x)},
            )
            # for _, row in df.iterrows():
            #     print(f"用户名：{row['用户名']}，密码：{row['密码']}，角色：{row['角色']}")
            return {
                str(row["用户名"]): {  # 显式转换为Python字符串
                    "password": str(row["密码"]) if pd.notna(row["密码"]) else None,
                    "role": str(row["角色"]) if pd.notna(row["角色"]) else "user",
                }
                # 遍历 DataFrame 的每一行，返回一个 (index, row) 的元组，其中：
                # index 是行索引（在这个代码中没有使用，因此用 _ 占位
                # row 是一个 Series，表示当前行的数据
                for _, row in df.iterrows()
            }

        except Exception as e:
            raise RuntimeError(f"用户数据加载失败: {str(e)}")

    # 密码记录函数
    def _update_excel_password(self, username: str, new_password: str) -> bool:
        """执行Excel更新"""
        while self._lock:
            pass

        try:
            self._lock = True
            df = pd.read_excel(self.excel_path, dtype=str)  # 强制所有列为字符串类型
            df["密码"] = df["密码"].astype("string")  # 明确指定密码列类型
            df.loc[df["用户名"] == username, "密码"] = str(new_password)
            df.to_excel(self.excel_path, index=False, engine="openpyxl")
            return True
        except Exception as e:
            print(f"Excel更新失败: {str(e)}")
            return False
        finally:
            self._lock = False

    # 密码检查函数，并调用记录函数
    def update_password(self, username: str, new_password: str) -> bool:
        """带校验的密码更新"""
        if not isinstance(new_password, str):
            raise TypeError("密码必须是字符串")
        if len(new_password.strip()) < 6:  # 添加strip()处理空白字符
            raise ValueError("密码至少需要6位")
        return self._update_excel_password(username, new_password.strip())
