"""Tests for job_scraper/lib/llm.py — shared NIM LLM client."""

import os
import unittest
from unittest.mock import MagicMock, patch

import job_scraper.lib.llm as llm_module
from job_scraper.lib.llm import chat, get_model, reset_client


class ResetMixin(unittest.TestCase):
    """Discard the cached client before and after each test."""

    def setUp(self):
        reset_client()

    def tearDown(self):
        reset_client()


class TestGetModel(ResetMixin):
    def test_default_model(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIM_MODEL", None)
            self.assertEqual(get_model(), "meta/llama-3.2-11b-vision-instruct")

    def test_env_override(self):
        with patch.dict(os.environ, {"NIM_MODEL": "openai/custom-model"}):
            self.assertEqual(get_model(), "openai/custom-model")


class TestClientCreation(ResetMixin):
    def test_missing_api_key_raises_environment_error(self):
        env = {k: v for k, v in os.environ.items() if k != "NVIDIA_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(EnvironmentError) as ctx:
                chat([{"role": "user", "content": "hi"}])
        self.assertIn("NVIDIA_API_KEY", str(ctx.exception))

    def test_empty_api_key_raises_environment_error(self):
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "   "}):
            with self.assertRaises(EnvironmentError):
                chat([{"role": "user", "content": "hi"}])

    def test_client_uses_nim_base_url(self):
        """Client must be constructed with the NIM base URL, not api.openai.com."""
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-test"}):
            os.environ.pop("NIM_BASE_URL", None)
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response("ok")
                chat([{"role": "user", "content": "hi"}])
                mock_openai.assert_called_once()
                _, kwargs = mock_openai.call_args
                self.assertIn("integrate.api.nvidia.com", kwargs.get("base_url", ""))

    def test_client_uses_custom_base_url(self):
        custom = "https://my-nim-host/v1"
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-test", "NIM_BASE_URL": custom}):
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response("ok")
                chat([{"role": "user", "content": "hi"}])
                _, kwargs = mock_openai.call_args
                self.assertEqual(kwargs.get("base_url"), custom)

    def test_client_is_reused_across_calls(self):
        """_get_client() must not instantiate OpenAI more than once."""
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-test"}):
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response("a")
                chat([{"role": "user", "content": "first"}])
                chat([{"role": "user", "content": "second"}])
                self.assertEqual(mock_openai.call_count, 1)


class TestChat(ResetMixin):
    def _make_env(self):
        return {"NVIDIA_API_KEY": "nvapi-test"}

    def test_returns_reply_string(self):
        with patch.dict(os.environ, self._make_env()):
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response("Hello!")
                result = chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "Hello!")

    def test_passes_messages_to_api(self):
        messages = [{"role": "user", "content": "rank this job"}]
        with patch.dict(os.environ, self._make_env()):
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response("7/10")
                chat(messages)
                call_kwargs = mock_instance.chat.completions.create.call_args[1]
                self.assertEqual(call_kwargs["messages"], messages)

    def test_default_model_sent_to_api(self):
        with patch.dict(os.environ, self._make_env()):
            os.environ.pop("NIM_MODEL", None)
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response("ok")
                chat([{"role": "user", "content": "hi"}])
                call_kwargs = mock_instance.chat.completions.create.call_args[1]
                self.assertEqual(call_kwargs["model"], "meta/llama-3.2-11b-vision-instruct")

    def test_model_override_per_call(self):
        with patch.dict(os.environ, self._make_env()):
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response("ok")
                chat([{"role": "user", "content": "hi"}], model="openai/other-model")
                call_kwargs = mock_instance.chat.completions.create.call_args[1]
                self.assertEqual(call_kwargs["model"], "openai/other-model")

    def test_empty_content_returns_empty_string(self):
        with patch.dict(os.environ, self._make_env()):
            with patch("openai.OpenAI") as mock_openai:
                mock_instance = MagicMock()
                mock_openai.return_value = mock_instance
                mock_instance.chat.completions.create.return_value = _fake_response(None)
                result = chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_response(content):
    """Build a minimal mock that looks like an openai ChatCompletion response."""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


if __name__ == "__main__":
    unittest.main()
