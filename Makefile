.PHONY: test check format demo regtest regtest-spark

test:
	poetry run pytest -q

check:
	poetry run ruff check cashu tests examples
	poetry run ruff format --check cashu tests examples
	poetry run mypy cashu

format:
	poetry run ruff check --fix cashu tests examples
	poetry run ruff format cashu tests examples

demo:
	poetry run python examples/demo.py

regtest:
	poetry run python tests/regtest/run.py

regtest-spark:
	poetry run python tests/regtest/spark.py
