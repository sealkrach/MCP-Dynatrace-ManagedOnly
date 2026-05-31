FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dynatrace_bridge_mcp.py .
EXPOSE 8000
ENV MCP_TRANSPORT=streamable-http
CMD ["python", "dynatrace_bridge_mcp.py"]
