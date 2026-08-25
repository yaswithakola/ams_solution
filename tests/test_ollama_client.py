"""
Small offline checks for Ollama response cleanup.
"""
from common.ollama_client import _load_json


def test_ollama_json_parser_handles_plain_json():
    result = _load_json('{"request_type": "report_generation", "confidence": 0.9}')
    assert result["request_type"] == "report_generation"
    assert result["confidence"] == 0.9
    print("test_ollama_json_parser_handles_plain_json: PASSED")


def test_ollama_json_parser_strips_qwen_thinking():
    raw = '<think>reasoning hidden</think>\n{"ok": true}'
    assert _load_json(raw) == {"ok": True}
    print("test_ollama_json_parser_strips_qwen_thinking: PASSED")


if __name__ == "__main__":
    test_ollama_json_parser_handles_plain_json()
    test_ollama_json_parser_strips_qwen_thinking()
    print("All Ollama client tests passed.")
