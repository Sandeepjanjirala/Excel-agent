from __future__ import annotations

from typing import TypedDict, Optional
from django.conf import settings
from langgraph.graph import StateGraph, START, END

from . import gemini_client
from .sandbox import run_pandas_snippet


class AgentState(TypedDict):
    question: str
    schema_text: str
    business_rules_text: str
    available_vars: list[str]
    dataframes: dict  # sheet_name -> DataFrame, not serialized to state graph output
    api_key: Optional[str]
    code: Optional[str]
    exec_status: Optional[str]      # "ok" | "error"
    raw_result: Optional[object]
    error: Optional[str]
    attempts: int
    final_answer: Optional[str]


def _generate_code_node(state: AgentState) -> AgentState:
    code = gemini_client.generate_pandas_code(
        question=state["question"],
        schema_text=state["schema_text"],
        business_rules_text=state["business_rules_text"],
        available_vars=state["available_vars"],
        previous_error=state.get("error"),
        previous_code=state.get("code"),
        api_key=state.get("api_key"),
    )
    return {**state, "code": code, "attempts": state["attempts"] + 1}


def _execute_node(state: AgentState) -> AgentState:
    status, result = run_pandas_snippet(
        state["code"], state["dataframes"], timeout=settings.SANDBOX_TIMEOUT_SECONDS
    )
    if status == "ok":
        return {**state, "exec_status": "ok", "raw_result": result, "error": None}
    return {**state, "exec_status": "error", "error": str(result)}


def _should_retry(state: AgentState) -> str:
    if state["exec_status"] == "ok":
        return "success"
    if state["attempts"] >= settings.MAX_CODEGEN_RETRIES + 1:
        return "give_up"
    return "retry"


def _phrase_answer_node(state: AgentState) -> AgentState:
    answer = gemini_client.phrase_answer(
        question=state["question"],
        raw_result=state["raw_result"],
        code_used=state["code"],
        api_key=state.get("api_key"),
    )
    return {**state, "final_answer": answer}


def _give_up_node(state: AgentState) -> AgentState:
    msg = (
        "I wasn't able to compute a reliable answer to that question. "
        f"Last error: {state.get('error')}"
    )
    return {**state, "final_answer": msg}


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("generate_code", _generate_code_node)
    builder.add_node("execute", _execute_node)
    builder.add_node("phrase_answer", _phrase_answer_node)
    builder.add_node("give_up", _give_up_node)

    builder.add_edge(START, "generate_code")
    builder.add_edge("generate_code", "execute")
    builder.add_conditional_edges(
        "execute",
        _should_retry,
        {"success": "phrase_answer", "retry": "generate_code", "give_up": "give_up"},
    )
    builder.add_edge("phrase_answer", END)
    builder.add_edge("give_up", END)

    return builder.compile()


def answer_question(
    question: str,
    schema_text: str,
    business_rules_text: str,
    dataframes: dict,
    api_key: str | None = None,
) -> dict:
    """Runs the full pipeline once and returns a dict with the final answer,
    the code that produced it, and how many attempts it took."""
    available_vars = list(dataframes.keys())
    if len(dataframes) == 1:
        available_vars = available_vars + ["df"]

    app = build_graph()
    initial_state: AgentState = {
        "question": question,
        "schema_text": schema_text,
        "business_rules_text": business_rules_text,
        "available_vars": available_vars,
        "dataframes": dataframes,
        "api_key": api_key,
        "code": None,
        "exec_status": None,
        "raw_result": None,
        "error": None,
        "attempts": 0,
        "final_answer": None,
    }
    result = app.invoke(initial_state)
    return {
        "answer": result["final_answer"],
        "code": result.get("code"),
        "raw_result": result.get("raw_result"),
        "attempts": result.get("attempts"),
        "status": result.get("exec_status"),
    }
