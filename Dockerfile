FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
COPY app ./app
COPY web ./web
COPY scripts ./scripts
ENV OWE_DATA_DIR=/data
EXPOSE 8000
VOLUME /data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
