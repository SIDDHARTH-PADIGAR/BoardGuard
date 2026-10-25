$ErrorActionPreference = "Stop"
python -m pip install -e ".[test]"
boardguard demo
python -m pytest
boardguard benchmark --repeats 3 --output evaluation/latest.json
