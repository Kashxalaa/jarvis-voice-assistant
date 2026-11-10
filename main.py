import sys

from brain.brains.claude import ClaudeBrain
from brain.brains.gemini import GeminiBrain
from brain.brains.groq import GroqBrain
from brain.conversation import Brains, run_turn
from brain.gemini_client import GeminiClient
from brain.memory import init_db
from config import (
    resolve_api_key,
    resolve_claude_cli_path,
    resolve_claude_model,
    resolve_claude_oauth_token,
    resolve_database_url,
    resolve_groq_api_key,
    resolve_groq_model,
    resolve_models,
    resolve_show_brain,
)


def repl(conn, client, get_input=input, emit=print, brains: Brains | None = None) -> None:
    emit("JARVIS is online. Type 'exit' or press Ctrl+C to quit.")
    try:
        while True:
            user_message = get_input("you: ").strip()
            if not user_message:
                continue
            if user_message.lower() in ("exit", "quit"):
                break
            reply = run_turn(conn, client, user_message, brains=brains)
            emit(f"jarvis: {reply}")
    except (KeyboardInterrupt, EOFError):
        pass
    emit("\nGoodbye.")


def main() -> None:
    api_key = resolve_api_key()
    if not api_key:
        print(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your "
            "free Gemini API key from https://aistudio.google.com/apikey"
        )
        sys.exit(1)

    try:
        models = resolve_models()
    except ValueError as exc:
        print(f"Configuration problem: {exc}. Check your .env file.")
        sys.exit(1)

    conn = init_db(resolve_database_url())
    client = GeminiClient(api_key=api_key, models=models)

    groq_api_key = resolve_groq_api_key()
    groq_model = resolve_groq_model()
    brains = Brains(
        gemini=GeminiBrain(client),
        claude=ClaudeBrain(
            resolve_claude_oauth_token(),
            resolve_claude_model(),
            cli_path=resolve_claude_cli_path(),
        ),
        groq=GroqBrain(groq_api_key, groq_model) if groq_api_key else None,
        groq_api_key=groq_api_key,
        groq_model=groq_model,
        show_tag=resolve_show_brain(),
    )

    repl(conn, client, brains=brains)


if __name__ == "__main__":
    main()
