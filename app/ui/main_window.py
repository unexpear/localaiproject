import os

from app.memory import local_memory
from app.services.command_dispatcher import dispatch_input
from app.services.model_service import prompt_for_missing_models, startup_summary


def _input_prompt(session_id):
    context = local_memory.get_session_context(session_id=session_id)
    workspace_path = context.get("workspace_path")
    if not workspace_path:
        return f"\nYou ({session_id}): "

    workspace_name = os.path.basename(workspace_path.rstrip("\\/")) or workspace_path
    return f"\nYou ({session_id} @ {workspace_name}): "


def main():
    print(startup_summary())
    prompt_for_missing_models()
    session_id = "default"

    while True:
        try:
            cmd = input(_input_prompt(session_id))
        except EOFError:
            print("\nShutting down.")
            break
        except KeyboardInterrupt:
            print("\nShutting down.")
            break

        event = dispatch_input(cmd, session_id=session_id)
        session_id = event.get("session_id", session_id)
        if event.get("output"):
            print(event["output"])
        if event.get("action") == "exit":
            break

if __name__ == "__main__":
    main()
