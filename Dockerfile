# 1. 使用一个轻量的Python官方镜像
FROM python:3.11-slim

# 2. 在容器内创建一个工作目录
WORKDIR /app

# 3. 优化构建缓存：先复制并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 复制应用运行所需的所有文件和目录
#    我们将保持与您本地一致的目录结构
COPY src/ ./src/
COPY data/ ./data/
COPY img/ ./img/

# 5. 复制根目录下的所有配置文件
COPY config_service.json .
COPY overview_config.json .
COPY project_overview_config.json .
COPY project_summary.json .
COPY project_summary.xlsx .

# 6. 定义容器启动时要执行的命令
#    因为主程序在 src 目录中，所以我们指定路径运行
CMD ["python", "src/main.py"]
