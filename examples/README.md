# Demo support agent

`agent.py` is a small support agent traced with the Traceloom SDK. A planner model picks tools
(`search`, `calculator`, `lookup_order`) and then writes a final answer. `lookup_order` always
fails, so each run produces one trace with:

- an `agent` span at the root, a `chain` span per step, then generations and `tool` spans below
  (three or more levels),
- one tool span at level ERROR (`lookup_order`),
- generations with model names, token usage and time to first token.

## Set up

```bash
traceloom seed-demo          # prints a demo login, project id and API keys
.venv/bin/uvicorn traceloom.main:app --port 8000
```

`seed-demo` is safe to run again: it prints the same keys each time.

## Run offline (default)

No network and no API key needed. A built-in fake model (`fake_llm.py`) supplies scripted
decisions, realistic model names (`claude-haiku-4-5-20251001`, `claude-sonnet-5-5`), usage and TTFT.

```bash
TRACELOOM_HOST=http://127.0.0.1:8000 \
TRACELOOM_PUBLIC_KEY=<public key from seed-demo> \
TRACELOOM_SECRET_KEY=<secret key from seed-demo> \
python examples/agent.py
```

Open `/project/<project id>/traces` and sign in with the demo login. The trace appears within a
few seconds. Without keys the agent still runs and exits 0, with tracing off.

## Run against Anthropic

Set `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`, default `claude-haiku-4-5-20251001`)
and run the same command. The agent then calls Anthropic through `traceloom_sdk.anthropic.wrap`,
so each model call is recorded as a generation by the wrapper.

```bash
pip install anthropic
ANTHROPIC_API_KEY=sk-ant-... python examples/agent.py --question "Where is order A-1042?"
```
