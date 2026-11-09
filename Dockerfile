# One image for every service of a cloud deployment (ADR 031): the API, the planner, the executor,
# the tool servers (python -m ops.run_all) and OPA (as a sidecar, from the same image). What each
# container runs is chosen by its command; see deploy/modules/<cloud>/apps.
#
#   docker build -t governed-agentic-ai:<version> .
#
# Needs uv.lock to include the "deploy" group: run `uv lock` once after pulling ADR 031.
# The sample databases are built from the repository's synthetic data only (Synthea sample, the
# hand-written demo patients, the fictional county). No real patient data belongs in an image.

FROM openpolicyagent/opa:1.21.1-static AS opa
FROM ghcr.io/astral-sh/uv:0.9.5 AS uv

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 governed
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=opa /opa /usr/local/bin/opa

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group deploy --no-install-project \
    && uv pip install --python /opt/venv/bin/python \
       https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl

COPY . .
RUN python profiles/clinic-assistant/data/build_db.py --csv profiles/clinic-assistant/data/sample_csv \
        --out profiles/clinic-assistant/data/clinic.db \
    && python profiles/clinic-assistant/data/build_db.py --add-chart --out profiles/clinic-assistant/data/clinic.db \
    && python -c "import sqlite3; sqlite3.connect('profiles/county-benefits/data/county.db').executescript(open('profiles/county-benefits/data/seed.sql').read())" \
    && mkdir -p /app/logs && chown -R governed /app/logs

USER governed
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
