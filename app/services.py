# Service layer for UI decoupling

from app.models.runtime_client import detect_ollama, list_ollama_models, run_model
from app.models.install_manager import ensure_default_models
from app.memory.local_memory import append_message, load_memory
from app.supervisor.router import route
from app.models.model_registry import get_model_for_role
from app.hardware import summary as hardware_summary
from app.ui.model_status_panel import show_model_status

class ModelService:
    @staticmethod
    def get_status():
        return detect_ollama(), list_ollama_models()

    @staticmethod
    def ensure_defaults(installed):
        return ensure_default_models(installed)


class MemoryService:
    @staticmethod
    def load(session_id="default"):
        return load_memory(session_id=session_id)

    @staticmethod
    def append(role, content, model=None, session_id="default"):
        append_message(role, content, model=model, session_id=session_id)

    @staticmethod
    def list_sessions():
        return list_sessions()

    @staticmethod
    def create_session(session_id):
        create_session(session_id)



class ChatService:
    @staticmethod
    def route(msg):
        return route(msg)

    @staticmethod
    def get_model_for_lane(lane):
        return get_model_for_role(lane)

    @staticmethod
    def infer(model, msg):
        return run_model(model, msg)

class HardwareService:
    @staticmethod
    def summary():
        return hardware_summary()

class StatusService:
    @staticmethod
    def show():
        show_model_status()
