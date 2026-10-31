"""Tier 1+2 terminal chat over Tier 4 memory: talk to the Learning coach.

The same brain the Tier 3 web voice wrap calls (POST /api/coach/ask).
History persists per session id and survives restarts; /forget wipes it.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe scripts\\learning_chat.py [session-id]
Type /quit to exit, /forget to erase this session, /stop + /arm for the
Tier 6 kill switch. MOCK_META=true (or no key) rehearses offline.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from backend.app.adapters import MetaAPIError, mock_meta_enabled  # noqa: E402
from backend.app.adapters.muse_adapter import MetaClaudeAdapter  # noqa: E402
from backend.app.coach import (  # noqa: E402
    CoachKilledError,
    CoachSession,
    coach_killed,
    forget_coach_history,
    set_killswitch,
)


def main() -> None:
    sid = sys.argv[1] if len(sys.argv) > 1 else "terminal"
    Muse = MetaClaudeAdapter()
    session = CoachSession(Muse=Muse, session_id=sid)
    mode = "MOCK (offline rehearsal)" if Muse.mock else "LIVE (%s)" % Muse.model
    print("FundiLoop Learning coach [%s] session '%s'. Type /quit to exit."
          % (mode, session.session_id))
    if coach_killed():
        print("The kill switch is tripped — /arm to resume, /quit to exit.")
    if session.history:
        print("Restored %d earlier turn(s) — /forget erases them."
              % (len(session.history) // 2))
    print("Try: teach me step 4 | what is the BME280? | hello")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye — this chat stays saved under '%s'." % session.session_id)
            break
        if not text:
            continue
        if text == "/quit":
            print("Goodbye — this chat stays saved under '%s'." % session.session_id)
            break
        if text == "/forget":
            session.history.clear()
            forget_coach_history(session.session_id)
            print("Forgotten — starting fresh.")
            continue
        if text == "/arm":
            set_killswitch(False, actor="terminal")
            print("Re-armed — the coach answers again.")
            continue
        if text == "/stop":
            set_killswitch(True, actor="terminal")
            print("Stopped — /arm to resume.")
            continue
        try:
            pending = None
            for event in session.ask_stream(text):
                kind = event["event"]
                if kind == "tool_call":
                    print("[looking up %s...]" % event["name"])
                elif kind == "token":
                    print(event["text"], end="", flush=True)
                elif kind == "confirmation_pending":
                    pending = event
                elif kind == "done":
                    print()
                    pending = event.get("confirmation_pending")
            while pending:
                try:
                    answer = input("%s [y/n] " % pending["question"]).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\nCancelled — the gated tool did not run.")
                    pending = None
                    break
                out = session.confirm(pending["call_id"], answer in ("y", "yes"))
                print(out["reply"])
                pending = out["confirmation_pending"]
        except CoachKilledError:
            print("Stopped by the kill switch — /arm to resume.")
        except MetaAPIError as exc:
            print("I could not reach the model: %s" % exc)
            print("Check META_API_KEY and the network, or set MOCK_META=true, then try again.")
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad turn
            print("That turn failed: %s" % exc)


if __name__ == "__main__":
    if mock_meta_enabled() is False and not os.environ.get("META_API_KEY"):
        print("Note: MOCK_META=false but META_API_KEY is empty — turns will fail cleanly.")
    main()
