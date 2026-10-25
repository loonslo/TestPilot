"""The two messages every run starts with: how to work, and what this task is."""

from __future__ import annotations

from api_test_agent.budget import Limits
from api_test_agent.spec import Spec
from api_test_agent.tools import TaskScope

SYSTEM_PROMPT = """You are an API test engineer. You test a personal bookkeeping REST service through four tools: search_spec, get_operation, http_call and submit_case. You cannot see the service's code.

How to work:
1. Read the rules in scope. Each rule states what the service must do. Judge the service only by those rules and the notes you are given; never invent expectations.
2. Use http_call to prepare your own data and watch the service's answers. Every call costs budget, so plan before you call. Callers A and B are two different users, anonymous sends no login, invalid_token sends a wrong token.
3. Compare what the service does with each rule.
4. For every behaviour that contradicts a rule, call submit_case with a self-contained case that states what the RULE requires, so the case passes on a correct service and fails on a faulty one. The case creates its own data in its steps. Do not submit a case unless you saw a real contradiction.
5. When you are done, reply with a short final report as plain text and no tool call: for each rule, what you checked and whether you found a problem. Saying that you found nothing is a valid result.

Be economical: you have a fixed number of model calls and HTTP calls, and the last model call must be your final report."""


def first_message(scope: TaskScope, spec: Spec, limits: Limits) -> str:
    operations = "\n".join(
        f"- {op.id}: {op.method} {op.path} - {op.summary}"
        for op in (spec.operations[name] for name in scope.operations)
    )
    return f"""Task {scope.task_id}: {scope.title}

{scope.brief}

Rules in scope:
{spec.rules_text(scope.rules)}

Notes on the service's answers:
{spec.conventions_text()}

Operations you may use (call get_operation for the details of one):
{operations}

Callers you may use: {", ".join(scope.callers)}

Limits: at most {limits.max_model_calls} model calls (this message is the start of the first), {limits.max_http_calls} HTTP calls and {int(limits.max_seconds)} seconds. Finish with your final report before they run out.

Begin."""
