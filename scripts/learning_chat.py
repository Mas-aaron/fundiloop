"""Tier 1+2 terminal chat: talk to the Learning coach in plain text.

The same brain voice will wrap in Tier 3. History lives in memory only:
restart and it forgets everything (expected until Tier 4).

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe scripts\\learning_chat.py
Type /quit to exit. MOCK_META=true (or no key) rehearses offline.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from backend.app.adapters import MetaAPIError, mock_meta_enabled  # noqa: E402
from backend.app.adapters.muse_adapter import MetaClaudeAdapter  # noqa: E402
from backend.app.coach import CoachSession  # noqa: E402


def main() -> None:
    Muse = MetaClaudeAdapter()
    session = CoachSession(Muse=Muse)
    mode = "MOCK (offline rehearsal)" if Muse.mock else "LIVE (%s)" % Muse.model
    print("FundiLoop Learning coach [%s]. Type /quit to exit." % mode)
    print("Try: teach me step 4 | what is the BME280? | hello")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye — your build notes are in the YAML, I keep nothing.")
            break
        if not text:
            continue
        if text == "/quit":
            print("Goodbye — your build notes are in the YAML, I keep nothing.")
            break
        try:
            for event in session.ask_stream(text):
                kind = event["event"]
                if kind == "tool_call":
                    print("[looking up %s...]" % event["name"])
                elif kind == "token":
                    print(event["text"], end="", flush=True)
                elif kind == "done":
                    print()
        except MetaAPIError as exc:
            print("I could not reach the model: %s" % exc)
            print("Check META_API_KEY and the network, or set MOCK_META=true, then try again.")
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad turn
            print("That turn failed: %s" % exc)


if __name__ == "__main__":
    if mock_meta_enabled() is False and not os.environ.get("META_API_KEY"):
        print("Note: MOCK_META=false but META_API_KEY is empty — turns will fail cleanly.")
    main()
