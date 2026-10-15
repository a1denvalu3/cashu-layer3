.PHONY: test check format demo regtest regtest-spark wallet wallet-check wallet-browser

test:
	poetry run pytest -q

check:
	poetry run ruff check tmp_cash tests examples
	poetry run ruff format --check tmp_cash tests examples
	poetry run mypy tmp_cash

format:
	poetry run ruff check --fix tmp_cash tests examples
	poetry run ruff format tmp_cash tests examples

demo:
	poetry run python examples/demo.py

regtest:
	poetry run python tests/regtest/run.py

regtest-spark:
	poetry run python tests/regtest/spark.py

wallet:
	cd web-wallet && npm run dev

wallet-check:
	cd web-wallet && npm run build && npm test

wallet-browser:
	cd web-wallet && npm run test:e2e
