import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screen_agent.understand.chat import (
    MultiBackendChatClient,
    OpenAICompatibleChatClient,
    build_chat_client,
)
from screen_agent.voice.sherpa_engine import build_keywords_txt


class MultiBackendTests(unittest.TestCase):
    def _client(self, base_url: str, model: str) -> OpenAICompatibleChatClient:
        return OpenAICompatibleChatClient(
            base_url=base_url, model=model, api_key="k", fallback_model=""
        )

    def test_fallback_to_second_backend(self) -> None:
        ok = self._client("http://ok", "m")

        def fake_complete(messages, system=None):
            return "hello"

        ok.complete = fake_complete  # type: ignore

        def bad_complete(messages, system=None):
            raise RuntimeError("down")

        bad = self._client("http://bad", "m")
        bad.complete = bad_complete  # type: ignore

        multi = MultiBackendChatClient([bad, ok])
        self.assertEqual(multi.complete([{"role": "user", "content": "hi"}]), "hello")

    def test_all_fail_raises(self) -> None:
        def bad_complete(messages, system=None):
            raise RuntimeError("down")

        bad1 = self._client("http://a", "m")
        bad1.complete = bad_complete  # type: ignore
        bad2 = self._client("http://b", "m")
        bad2.complete = bad_complete  # type: ignore
        multi = MultiBackendChatClient([bad1, bad2])
        with self.assertRaises(RuntimeError):
            multi.complete([{"role": "user", "content": "hi"}])

    def test_build_from_config(self) -> None:
        cfg = {
            "enabled": True,
            "backends": [
                {"base_url": "http://127.0.0.1:8000/v1", "model": "Qwen/Qwen2.5-7B-Instruct"},
                {
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                    "api_key_env": "UNITTEST_DEEPSEEK_KEY",
                },
            ],
        }
        with patch.dict("os.environ", {"UNITTEST_DEEPSEEK_KEY": "sk-x"}):
            client = build_chat_client(cfg)
        self.assertIsInstance(client, MultiBackendChatClient)
        assert isinstance(client, MultiBackendChatClient)
        self.assertEqual(len(client.backends), 2)
        self.assertEqual(client.backends[1].api_key, "sk-x")

    def test_disabled(self) -> None:
        from screen_agent.understand.chat import DisabledChatClient

        self.assertIsInstance(build_chat_client({"enabled": False, "backends": []}), DisabledChatClient)


class KeywordBuildTests(unittest.TestCase):
    def test_keywords_format(self) -> None:
        import tempfile

        tokens = "\n".join(
            [
                "<blk> 0",
                "x 100",
                "iǎo 101",
                "g 102",
                "uāng 103",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            kws_dir = Path(tmp)
            (kws_dir / "tokens.txt").write_text(tokens, encoding="utf-8")
            out = build_keywords_txt(["小光", "光光"], kws_dir, kws_dir / "keywords.txt", 0.4)
            content = out.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(content[0], "x iǎo g uāng @小光")
        self.assertEqual(content[1], "g uāng g uāng @光光")

    def test_unknown_char_raises(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            kws_dir = Path(tmp)
            (kws_dir / "tokens.txt").write_text("<blk> 0\nx 100\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_keywords_txt(["小光"], kws_dir, kws_dir / "keywords.txt", 0.4)


if __name__ == "__main__":
    unittest.main()


class StreamTests(unittest.TestCase):
    def _client(self, base_url: str, model: str) -> OpenAICompatibleChatClient:
        return OpenAICompatibleChatClient(
            base_url=base_url, model=model, api_key="k", fallback_model=""
        )

    def test_complete_stream_accumulates(self) -> None:
        client = self._client("http://ok", "m")

        def fake_stream(model, messages, on_delta):
            parts = ["你", "好", "，", "世界"]
            full = ""
            for part in parts:
                full += part
                if on_delta:
                    on_delta(full)
            return full

        client._request_stream = fake_stream  # type: ignore[attr-defined]
        accumulated: list[str] = []
        reply = client.complete_stream(
            [{"role": "user", "content": "hi"}], on_delta=accumulated.append
        )
        self.assertEqual(reply, "你好，世界")
        self.assertEqual(accumulated[-1], "你好，世界")
        self.assertEqual(len(accumulated), 4)

    def test_complete_stream_falls_back_to_non_stream(self) -> None:
        client = self._client("http://ok", "m")

        def bad_stream(model, messages, on_delta):
            raise RuntimeError("stream unsupported")

        client._request_stream = bad_stream  # type: ignore[attr-defined]
        client.complete = lambda messages, system=None: "非流式兜底"  # type: ignore[method-assign]
        reply = client.complete_stream([{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "非流式兜底")

    def test_multi_backend_stream_fallback(self) -> None:
        ok = self._client("http://ok", "m")

        def fake_stream(model, messages, on_delta):
            full = "回答"
            if on_delta:
                on_delta(full)
            return full

        ok._request_stream = fake_stream  # type: ignore[attr-defined]

        bad = self._client("http://bad", "m")
        bad._request_stream = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore[attr-defined]

        multi = MultiBackendChatClient([bad, ok])
        got: list[str] = []
        reply = multi.complete_stream(
            [{"role": "user", "content": "hi"}], on_delta=got.append
        )
        self.assertEqual(reply, "回答")
        self.assertEqual(got, ["回答"])
