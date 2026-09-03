# SAR drafter - common tasks. The offline targets need no credentials.

.PHONY: help test eval run run-trace deploy web events teardown clean

help:
	@echo "Offline (no AWS needed):"
	@echo "  make test        - run unit tests (mock provider)"
	@echo "  make eval        - run the evaluation harness (mock provider)"
	@echo "  make run         - draft a SAR for the bundled case (mock)"
	@echo "  make run-trace   - same, showing the investigation trace"
	@echo "AWS (needs credentials + Bedrock access):"
	@echo "  make deploy      - IAM role + DynamoDB + Lambda"
	@echo "  make web         - Function URL + CloudFront (PUBLIC - see README security note)"
	@echo "  make events      - S3 bucket + EventBridge auto-draft"
	@echo "  make teardown    - remove ALL AWS resources created by this project"

test:
	python tests/test_sar_drafter.py

eval:
	python eval/run_eval.py

run:
	python run.py --provider mock

run-trace:
	python run.py --provider mock --show-trace

deploy:
	python deploy/deploy.py

web:
	python deploy/deploy_web.py

events:
	python deploy/deploy_events.py

teardown:
	python deploy/teardown.py

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f out.json
