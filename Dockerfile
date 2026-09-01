FROM python:3.12-slim
ARG BUILD_NUMBER=0
ENV PAGERMCP_BUILD=$BUILD_NUMBER
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pager_mcp pager_mcp
COPY README.md RFC-0001-pager-protocol.md /app/
ENV MODE=server PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "if [ \"$MODE\" = client ]; then exec python -m pager_mcp.client; else exec python -m pager_mcp.server; fi"]
