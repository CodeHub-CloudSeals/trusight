CORPUS ?= ./corpus
export PYTHONPATH := src

.PHONY: install test eval demo api lint deploy
install:
	pip install -r requirements-dev.txt
test:
	TRUSTSIGHT_CORPUS="$(CORPUS)" pytest -q
eval:
	python scripts/run_eval.py "$(CORPUS)"
demo:
	python scripts/demo_pile.py
api:
	TRUSTSIGHT_CORPUS="$(CORPUS)" uvicorn trustsight.api.main:app --reload
lint:
	ruff check src handlers scripts tests
deploy:
	cd infra && cdk deploy --context region=$${AWS_REGION:-eu-west-2}
