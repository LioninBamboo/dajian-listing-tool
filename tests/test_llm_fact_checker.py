import pytest
from unittest.mock import MagicMock
from src.utils.llm_fact_checker import llm_fact_check

def test_llm_fact_check_no_api_key(monkeypatch):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    violations = llm_fact_check("Title", "Desc", {}, "Gen Title", "Gen Desc")
    assert violations == []

def test_llm_fact_check_mock_violations(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "fake_key")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices[0].message.content = '''
    [
        {
            "quote": "NASA certified foam",
            "reason": "Not mentioned in source facts",
            "severity": "HIGH"
        }
    ]
    '''
    mock_client.chat.completions.create.return_value = mock_response

    class MockOpenAI:
        def __init__(self, **kwargs):
            self.chat = mock_client.chat

    monkeypatch.setattr("src.utils.llm_fact_checker.OpenAI", MockOpenAI)

    violations = llm_fact_check(
        source_title="Foam Mattress",
        source_description="A good foam mattress",
        source_specs={},
        generated_title="NASA Certified Foam Mattress",
        generated_description="NASA certified foam."
    )

    assert len(violations) == 1
    assert violations[0]["severity"] == "HIGH"
    assert "NASA" in violations[0]["quote"]

def test_llm_fact_check_no_violations(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "fake_key")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices[0].message.content = '[]'
    mock_client.chat.completions.create.return_value = mock_response

    class MockOpenAI:
        def __init__(self, **kwargs):
            self.chat = mock_client.chat

    monkeypatch.setattr("src.utils.llm_fact_checker.OpenAI", MockOpenAI)

    violations = llm_fact_check(
        source_title="Foam Mattress",
        source_description="A good foam mattress",
        source_specs={},
        generated_title="Foam Mattress",
        generated_description="A good foam mattress."
    )

    assert violations == []

def test_llm_fact_check_invalid_json(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "fake_key")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices[0].message.content = 'I am sorry, I cannot do this.'
    mock_client.chat.completions.create.return_value = mock_response

    class MockOpenAI:
        def __init__(self, **kwargs):
            self.chat = mock_client.chat

    monkeypatch.setattr("src.utils.llm_fact_checker.OpenAI", MockOpenAI)

    violations = llm_fact_check(
        source_title="Foam",
        source_description="A foam",
        source_specs={},
        generated_title="Foam",
        generated_description="Foam"
    )

    assert violations == []


def test_llm_fact_check_ignores_configured_store_boilerplate(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "fake_key")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices[0].message.content = '''
    [
        {
            "quote": "AQUAVERVE",
            "reason": "Not present in supplier facts",
            "severity": "MEDIUM"
        },
        {
            "quote": "✦ Ships from US Warehouse ✦",
            "reason": "Not present in supplier facts",
            "severity": "MEDIUM"
        },
        {
            "quote": "Quality Guaranteed • Fast US Shipping • Trusted Seller",
            "reason": "Not present in supplier facts",
            "severity": "MEDIUM"
        },
        {
            "quote": "NASA certified foam",
            "reason": "Not present in supplier facts",
            "severity": "HIGH"
        }
    ]
    '''
    mock_client.chat.completions.create.return_value = mock_response

    class MockOpenAI:
        def __init__(self, **kwargs):
            self.chat = mock_client.chat

    monkeypatch.setattr("src.utils.llm_fact_checker.OpenAI", MockOpenAI)

    violations = llm_fact_check(
        source_title="Foam Chair",
        source_description="A foam chair",
        source_specs={},
        generated_title="Foam Chair",
        generated_description="AQUAVERVE NASA certified foam",
    )

    assert [violation["quote"] for violation in violations] == ["NASA certified foam"]


def test_llm_fact_check_drops_quote_not_present_in_generated_copy(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "fake_key")
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices[0].message.content = '''
    [{
        "quote": "Crafted from high quality Iron",
        "reason": "Contradicts the source",
        "severity": "HIGH"
    }]
    '''
    mock_client.chat.completions.create.return_value = mock_response

    class MockOpenAI:
        def __init__(self, **kwargs):
            self.chat = mock_client.chat

    monkeypatch.setattr("src.utils.llm_fact_checker.OpenAI", MockOpenAI)

    violations = llm_fact_check(
        source_title="Gaming Table",
        source_description="Conflicted supplier description",
        source_specs={"Material": "Particle Board"},
        generated_title="Dining Gaming Table",
        generated_description="The removable top switches between dining and gaming configurations.",
    )

    assert violations == []
