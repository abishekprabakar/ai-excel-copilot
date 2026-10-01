.PHONY: install test dev-api dev-ui eval synth

install:
	cd backend && pip install -e ".[dev]"
	cd frontend && npm ci

test:
	cd backend && pytest
	cd frontend && npm test && npm run build
	cd infra && pytest test_stack.py

dev-api:   ## offline planner unless ANTHROPIC_API_KEY is set
	cd backend && uvicorn excel_copilot.api.main:app --reload --port 8000

dev-ui:
	cd frontend && npm run dev

eval:
	cd backend && excel-copilot eval evals/sales_ops.json

synth:
	cd frontend && npm run build
	cd infra && cdk synth
