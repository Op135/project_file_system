import logging
from pathlib import Path
from typing import Any, Dict

import pandas as pd

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


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
            except Exception:
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
        # {"password": "xxx", "role": "匿名用户"}
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
                    "role": str(row["角色"]) if pd.notna(row["角色"]) else "匿名用户",
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
        except Exception:
            logger.error("Excel更新失败", exc_info=True)
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

    # 统一的用户数据增、删、改函数
    def modify_user(self, action: str, username: str, password: str = "", role: str = "") -> bool:
        """
        执行用户数据的 Excel 更新操作
        :param action: 操作类型，可选值为 'add' (新增), 'update' (修改), 'delete' (删除)
        :param username: 用户名 (主键)
        :param password: 密码
        :param role: 角色
        """
        while self._lock:
            pass

        try:
            self._lock = True
            # 强制全部按字符串读取，防止 pandas 自动推导数据类型带来的格式问题
            df = pd.read_excel(self.excel_path, dtype=str)

            # 确保关键列存在，防止空表报错
            for col in ["用户名", "密码", "角色"]:
                if col not in df.columns:
                    df[col] = pd.Series(dtype="string")

            if action == "add":
                if username in df["用户名"].values:
                    raise ValueError(f"用户 {username} 已存在")
                new_row = pd.DataFrame(
                    [
                        {
                            "用户名": str(username),
                            "密码": str(password) if password else "",
                            "角色": str(role) if role else "普通用户",
                        }
                    ]
                )
                df = pd.concat([df, new_row], ignore_index=True)

            elif action in ["update", "edit"]:
                if username not in df["用户名"].values:
                    raise ValueError(f"用户 {username} 不存在")
                if password is not None:
                    df.loc[df["用户名"] == username, "密码"] = str(password)
                if role is not None:
                    df.loc[df["用户名"] == username, "角色"] = str(role)

            elif action == "delete":
                if username not in df["用户名"].values:
                    raise ValueError(f"用户 {username} 不存在")
                df = df[df["用户名"] != username]
            else:
                raise ValueError(f"未知的操作指令: {action}")

            # 写回 Excel
            df.to_excel(self.excel_path, index=False, engine="openpyxl")
            return True

        except Exception as e:
            logger.error(f"Excel用户数据 {action} 操作失败: {str(e)}", exc_info=True)
            raise e  # 将异常抛出以便在前端捕获并提示用户
        finally:
            self._lock = False
