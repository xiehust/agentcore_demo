FROM --platform=linux/arm64 python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ballast: ~950MB 随机数据 (不可压缩), 把镜像撑到 ~1GB, 用于测试大镜像冷启动
RUN mkdir -p /opt/ballast && \
    for i in 1 2 3 4 5 6 7 8 9 10; do \
      dd if=/dev/urandom of=/opt/ballast/blob_$i.bin bs=1M count=95 status=none; \
    done && ls -lh /opt/ballast/

COPY main.py .
EXPOSE 8080
CMD ["python", "main.py"]
