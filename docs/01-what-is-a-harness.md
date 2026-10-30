# What is an Agent Harness?

**Agent = Model + Harness**

The harness is the runtime system around a language model that turns reasoning into controlled, reliable action.

## Typical components

1. **Execution environment** — sandboxes, containers, allow-lists
2. **Tools & MCP** — schemas, descriptions, routing
3. **Context assembly** — what the model sees (system prompt, memory, retrieved evidence)
4. **State & memory** — session, long-term, working memory
5. **Orchestration / routing** — multi-agent, handoffs, control flow
6. **Guardrails & verification** — checks, refusal, human-in-the-loop, counterevidence
7. **Observability** — traces, metrics, audit logs

## Why it matters now

- Models are getting better at absorbing simple scaffolding.
- Reliability, cost, trust, efficiency and new frontiers still live in the harness.
- Recent research (Harness Learning, Harness², Agent-Reflex) shows harnesses can be *learned* and *recursively improved* at test time without weight updates.

## Design principles for our systems

- Harness is versioned, editable and auditable (like code).
- Prefer primary sources + independent checks over vendor claims.
- Zero new findings is a valid result.
- Separate model inference from harness control.
