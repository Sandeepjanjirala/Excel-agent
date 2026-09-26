"""
LangGraph execution graph for the Excel Agent.

Includes:
- Intelligent self-correction for runtime errors
- Result verification & reflection loop for suspiciously empty or NaN outputs
- Conversational history support for follow-up questions
"""
from __future__ import annotations

import math
from typing import TypedDict, Optional
from django.conf import settings
from langgraph.graph import StateGraph, START, END

from . import gemini_client
from .sandbox import run_pandas_snippet


class AgentState(TypedDict):
    question: str
    chat_history_text: Optional[str]
    schema_text: str
    business_rules_text: str
    available_vars: list[str]
    dataframes: dict
    api_key: Optional[str]
    code: Optional[str]
    exec_status: Optional[str]      # "ok" | "empty_result" | "error"
    raw_result: Optional[object]
    error: Optional[str]
    reflection_feedback: Optional[str]
    attempts: int
    final_answer: Optional[str]


def _is_suspiciously_empty(result) -> bool:
    """Check if the result is completely empty or null when data was expected."""
    if result is None:
        return True
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        return True
    if isinstance(result, (list, tuple, set)) and len(result) == 0:
        return True
    if isinstance(result, dict):
        if len(result) == 0:
            return True
        # If dict represents an empty DataFrame with column names but 0 rows
        if all(isinstance(v, (list, tuple, dict)) and len(v) == 0 for v in result.values()):
            return True
    return False


def _generate_code_node(state: AgentState) -> AgentState:
    code = gemini_client.generate_pandas_code(
        question=state["question"],
        schema_text=state["schema_text"],
        business_rules_text=state["business_rules_text"],
        available_vars=state["available_vars"],
        chat_history_text=state.get("chat_history_text"),
        previous_error=state.get("error"),
        previous_code=state.get("code"),
        reflection_feedback=state.get("reflection_feedback"),
        api_key=state.get("api_key"),
    )
    return {**state, "code": code, "attempts": state["attempts"] + 1}


def _execute_node(state: AgentState) -> AgentState:
    status, result = run_pandas_snippet(
        state["code"], state["dataframes"], timeout=settings.SANDBOX_TIMEOUT_SECONDS
    )
    if status == "ok":
        if _is_suspiciously_empty(result):
            feedback = (
                "The code executed without syntax error, but produced an EMPTY result (0 rows / empty list / NaN). "
                "Common causes to check: (1) string filter casing or whitespace (ensure you use .str.strip().str.lower()), "
                "(2) multi-table merge on too many columns (merge ONLY on the primary key e.g. 'Branch', using how='left'), "
                "(3) wrong column name. Please inspect the schema and fix your query."
            )
            return {
                **state,
                "exec_status": "empty_result",
                "raw_result": result,
                "error": None,
                "reflection_feedback": feedback,
            }
        return {
            **state,
            "exec_status": "ok",
            "raw_result": result,
            "error": None,
            "reflection_feedback": None,
        }
    return {
        **state,
        "exec_status": "error",
        "error": str(result),
        "reflection_feedback": None,
    }


def _should_retry(state: AgentState) -> str:
    if state["exec_status"] == "ok":
        return "success"
    if state["attempts"] >= settings.MAX_CODEGEN_RETRIES + 1:
        if state["exec_status"] == "empty_result":
            return "success"  # Allow phrasing to honestly state no records matched
        return "give_up"
    return "retry"


def _phrase_answer_node(state: AgentState) -> AgentState:
    answer = gemini_client.phrase_answer(
        question=state["question"],
        raw_result=state["raw_result"],
        code_used=state["code"],
        chat_history_text=state.get("chat_history_text"),
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
    chat_history_text: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Runs the full pipeline with reflection and conversation context."""
    available_vars = list(dataframes.keys())
    if len(dataframes) == 1:
        available_vars = available_vars + ["df"]

    app = build_graph()
    initial_state: AgentState = {
        "question": question,
        "chat_history_text": chat_history_text,
        "schema_text": schema_text,
        "business_rules_text": business_rules_text,
        "available_vars": available_vars,
        "dataframes": dataframes,
        "api_key": api_key,
        "code": None,
        "exec_status": None,
        "raw_result": None,
        "error": None,
        "reflection_feedback": None,
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
