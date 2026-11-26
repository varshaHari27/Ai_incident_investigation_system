import os
from dotenv import load_dotenv
from anthropic import AnthropicFoundry

# Load keys from the .env file
load_dotenv()

client = AnthropicFoundry(
    api_key=os.getenv("AZURE_ANTHROPIC_API_KEY"),
    base_url=os.getenv("AZURE_ANTHROPIC_ENDPOINT"),
)
MODEL = os.getenv("AZURE_CLAUDE_DEPLOYMENT")

DEFAULT_SYSTEM = "You are an expert at analyzing application logs and finding root causes of errors."


def _get_text(response) -> str:
    """Pull the text out of Claude's response."""
    return "".join(block.text for block in response.content if block.type == "text")


def ask_claude(prompt: str, system: str = DEFAULT_SYSTEM, max_tokens: int = 1500) -> str:
    """Send one prompt and get the reply. Used by the LangGraph steps."""
    response = client.messages.create(
        model=MODEL,
        system=system,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return _get_text(response)


def chat_claude(messages: list[dict], system: str = DEFAULT_SYSTEM, max_tokens: int = 1500) -> str:
    """Send a whole conversation and get the next reply. Used by the chatbot.

    messages looks like: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    """
    response = client.messages.create(
        model=MODEL,
        system=system,
        max_tokens=max_tokens,
        messages=messages,
    )
    return _get_text(response)


if __name__ == "__main__":
    # Quick test: run `python llm.py`
    print(ask_claude("Say hello in one short sentence."))