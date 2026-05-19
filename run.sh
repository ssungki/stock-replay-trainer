#!/bin/bash
# 주식 모의 백테스트 실행기 — 브라우저에서 자동으로 열린다.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/streamlit run app.py
