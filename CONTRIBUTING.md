# Contributing

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest -n auto --cov
```

The checks are tested against real servers on loopback (`tests/servers.py`: HTTP, TCP, a TLS server with a certificate
made by `openssl`), not against mocks; keep it that way. The decision logic is tested against a simulator with known
truth (`tests/sim.py`). After a change to `tracker.py`, run `python tools/evaluate.py --check`: it fails if an outage is
missed or invented. Do not tune on the seeds the report uses (1000 and above).

Claims in the docs are checked against `docs/report.json` by `tests/test_docs.py`. Re-run
`python tools/evaluate.py --out docs/report.json`, then `python tools/make_evaluation_doc.py`, and update the README
when the numbers change.
