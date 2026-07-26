from typing import Literal

APPROVAL_KEYWORDS = {"approve", "reject", "pass", "changes", "y", "n"}


def route_message(message: str) -> Literal["pipeline", "chat"]:
    """
    Route incoming chat message to either 'pipeline' (LangGraph command/approval, 0 tokens)
    or 'chat' (Claude LLM conversation with state context).
    """
    if not message:
        return "chat"

    cleaned_msg = message.strip()

    # Commands starting with '/' (e.g. /pm, /lint)
    if cleaned_msg.startswith("/"):
        return "pipeline"

    # Approval / Human-in-loop decision keywords
    if cleaned_msg.lower() in APPROVAL_KEYWORDS:
        return "pipeline"

    # Questions containing '?'
    if "?" in cleaned_msg:
        return "chat"

    # Default to chat
    return "chat"
