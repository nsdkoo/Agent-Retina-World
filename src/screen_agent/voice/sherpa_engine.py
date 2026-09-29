"""sherpa-onnx 语音引擎封装：流式 ASR / 关键词唤醒 / VITS TTS。

模型目录结构约定（models/ 下）：
- sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/   流式双语 ASR
- sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/        关键词唤醒
- vits-zh-hf-fanchen-C/                                         中文 TTS（女声）
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def _find_one(directory: Path, patterns: list[str]) -> Path:
    """按优先级在目录里找第一个存在的文件（优先 int8 量化版）。"""
    for pattern in patterns:
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]
    raise FileNotFoundError(f"在 {directory} 下找不到任何匹配 {patterns} 的文件")


class SherpaAsr:
    """流式语音识别（partial 中间结果 + endpoint 断句 + final 终稿）。"""

    def __init__(
        self,
        model_dir: Path,
        num_threads: int = 1,
        hotwords_file: Path | None = None,
    ) -> None:
        import sherpa_onnx

        if not model_dir.is_dir():
            raise FileNotFoundError(
                f"ASR 模型不存在: {model_dir}\n运行: python main.py voice --download-model"
            )
        encoder = _find_one(
            model_dir, ["*encoder*.int8.onnx", "*encoder*.onnx"]
        )
        decoder = _find_one(model_dir, ["*decoder*.onnx"])
        joiner = _find_one(model_dir, ["*joiner*.int8.onnx", "*joiner*.onnx"])
        tokens = model_dir / "tokens.txt"
        logger.info("加载 ASR 模型: %s", model_dir.name)
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(tokens),
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            num_threads=num_threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=1.2,
            rule2_min_trailing_silence=0.8,
            rule3_min_utterance_length=20,
            **({"hotwords_file": str(hotwords_file), "hotwords_score": 1.5}
               if hotwords_file and hotwords_file.exists() else {}),
        )

    def create_stream(self):
        return self.recognizer.create_stream()

    def accept(self, stream, samples) -> None:
        stream.accept_waveform(SAMPLE_RATE, samples)

    def decode(self, stream) -> None:
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)

    def partial(self, stream) -> str:
        return self.recognizer.get_result(stream)

    def is_endpoint(self, stream) -> bool:
        return self.recognizer.is_endpoint(stream)

    def finalize(self, stream) -> str:
        """结束当前语句并取出终稿文本。"""
        stream.input_finished()
        self.decode(stream)
        return self.recognizer.get_result(stream)


class SherpaKws:
    """关键词唤醒（wenetspeech zipformer KWS）。"""

    def __init__(
        self,
        model_dir: Path,
        keywords_file: Path,
        threshold: float = 0.4,
        num_threads: int = 1,
    ) -> None:
        import sherpa_onnx

        if not model_dir.is_dir():
            raise FileNotFoundError(
                f"KWS 模型不存在: {model_dir}\n运行: python main.py voice --download-model"
            )
        encoder = _find_one(model_dir, ["*encoder*.int8.onnx", "*encoder*.onnx"])
        decoder = _find_one(model_dir, ["*decoder*.onnx"])
        joiner = _find_one(model_dir, ["*joiner*.int8.onnx", "*joiner*.onnx"])
        tokens = model_dir / "tokens.txt"
        logger.info("加载 KWS 模型: %s", model_dir.name)
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(tokens),
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            keywords_file=str(keywords_file),
            num_threads=num_threads,
            sample_rate=SAMPLE_RATE,
            keywords_score=1.0,
            keywords_threshold=threshold,
        )

    def create_stream(self):
        return self.kws.create_stream()

    def accept(self, stream, samples) -> None:
        stream.accept_waveform(SAMPLE_RATE, samples)

    def decode(self, stream) -> str:
        """喂完音频后解码；命中关键词返回词文本，否则返回空串。"""
        hit = ""
        while self.kws.is_ready(stream):
            self.kws.decode_stream(stream)
            result = self.kws.get_result(stream)
            if result:
                hit = result
                self.kws.reset_stream(stream)
        return hit


def _ensure_ascii_path(path: Path) -> Path:
    """kaldifst 在 Windows 上读不了非 ASCII 路径，用目录联接绕过。"""
    try:
        str(path).encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    import tempfile

    link_root = Path(tempfile.gettempdir()) / "agent_retina_links"
    link_root.mkdir(parents=True, exist_ok=True)
    link = link_root / path.name
    if not link.exists():
        import _winapi

        _winapi.CreateJunction(str(path.resolve()), str(link))
    return link


class SherpaTts:
    """离线中文 TTS（vits），返回 float32 音频由调用方播放。"""

    def __init__(self, model_dir: Path, num_threads: int = 1, sid: int = 0) -> None:
        import sherpa_onnx

        model_dir = _ensure_ascii_path(model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"TTS 模型不存在: {model_dir}")
        onnx_model = _find_one(model_dir, ["*.onnx"])
        lexicon = model_dir / "lexicon.txt"
        tokens = _find_one(model_dir, ["tokens.txt"])
        dict_dir = model_dir / "dict"
        logger.info("加载 TTS 模型: %s", model_dir.name)
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(onnx_model),
            lexicon=str(lexicon) if lexicon.exists() else "",
            tokens=str(tokens),
            data_dir="",
            dict_dir=str(dict_dir) if dict_dir.is_dir() else "",
        )
        # 数字/日期/电话号码朗读规则（模型自带）
        rule_fsts = ",".join(
            str(p) for name in ("number.fst", "date.fst", "new_heteronym.fst", "phone.fst")
            if (p := model_dir / name).exists()
        )
        self.tts = sherpa_onnx.OfflineTts(
            sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(vits=vits),
                rule_fsts=rule_fsts,
                rule_fars="",
                max_num_sentences=1,
            )
        )
        self.sid = sid

    def synthesize(self, text: str) -> tuple[object, int]:
        """返回 (float32 samples, sample_rate)。"""
        audio = self.tts.generate(text, sid=self.sid, speed=1.0)
        return audio.samples, audio.sample_rate


def _contract_final(final: str) -> list[str]:
    """生成拼音缩写式候选：kws 模型的韵母表用 uì/uí 这类缩写（uei→ui、iou→iu、uen→un）。"""
    variants = [final]
    for full, short in (("ue", "u"), ("io", "i"), ("uen", "un")):
        if final.startswith(full):
            variants.append(short + final[len(full):])
    return list(dict.fromkeys(variants))


def _py_initial_final(char: str) -> list[tuple[str, str]]:
    """返回一个汉字的 (声母, 带调韵母) 候选列表（含缩写式）。"""
    from pypinyin import lazy_pinyin, Style

    candidates: list[tuple[str, str]] = []
    initials = lazy_pinyin(char, style=Style.INITIALS, strict=False)
    finals_tone = lazy_pinyin(char, style=Style.FINALS_TONE)
    finals_tone3 = lazy_pinyin(char, style=Style.FINALS_TONE3)
    for i, initial in enumerate(initials):
        finals: list[str] = []
        if i < len(finals_tone):
            finals.extend(_contract_final(finals_tone[i]))
        if i < len(finals_tone3):
            finals.extend(_contract_final(finals_tone3[i]))
        for final in dict.fromkeys(finals):
            candidates.append((initial, final))
    return candidates


def _token_match(token_set: set[str], token: str) -> str | None:
    """先精确匹配，再大小写不敏感兜底（tokens.txt 里拼音声母与英文字母同存）。"""
    if token in token_set:
        return token
    lowered = {t.lower(): t for t in token_set}
    return lowered.get(token.lower())


# 英文唤醒词 → KWS 音素序列（bilingual 模型 tokens.txt 实测存在；
# 同一词多变体覆盖不同发音/声调，KWS 命中任一即触发）
EN_PHONEMES: dict[str, list[list[str]]] = {
    "rita": [
        ["R", "ī", "t", "ē"],
        ["R", "ī", "t", "ā"],
        ["R", "ī", "t", "ǎ"],
        ["R", "ī", "t", "à"],
    ],
    "reta": [
        ["R", "ē", "t", "à"],
        ["R", "ī", "t", "ē"],
    ],
}


def build_keywords_txt(
    keywords: list[str],
    kws_model_dir: Path,
    out_path: Path,
    threshold: float = 0.4,
    extra_keyword_lines: list[str] | None = None,
) -> Path:
    """把中文唤醒词转成 sherpa KWS keywords.txt（声母+带调韵母 token）。

    行格式：x iǎo g uāng @小光
    任一 token 无法匹配 tokens.txt 时抛错，避免静默失灵。
    """
    tokens_path = kws_model_dir / "tokens.txt"
    if not tokens_path.exists():
        raise FileNotFoundError(f"tokens.txt 不存在: {tokens_path}")
    token_set = {
        line.split()[0]
        for line in tokens_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }

    lines: list[str] = []
    for word in keywords:
        parts: list[str] = []
        for char in word:
            if not char.strip():
                continue
            matched: tuple[str, str] | None = None
            for initial, final in _py_initial_final(char):
                if initial:
                    init_tok = _token_match(token_set, initial)
                    fin_tok = _token_match(token_set, final)
                    if init_tok and fin_tok:
                        matched = (init_tok, fin_tok)
                        break
                else:
                    fin_tok = _token_match(token_set, final)
                    if fin_tok:
                        matched = ("", fin_tok)
                        break
            if matched is None:
                raise ValueError(
                    f"唤醒词「{word}」的汉字「{char}」无法匹配 KWS tokens.txt，"
                    f"请换一个唤醒词"
                )
            initial_tok, final_tok = matched
            if initial_tok:
                parts.append(initial_tok)
            parts.append(final_tok)
        if parts:
            lines.append(f"{' '.join(parts)} @{word}")

    for line in extra_keyword_lines or []:
        tokens = line.split("@")[0].split()
        missing = [tk for tk in tokens if tk not in token_set]
        if missing:
            raise ValueError(
                f"英文唤醒词音素 {missing} 不在 KWS tokens.txt，已拒绝（防静默失灵）"
            )
        lines.append(line.strip())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    out_path.write_text(content, encoding="utf-8")
    logger.info("生成 keywords.txt:\n%s", content.strip())
    return out_path


class SenseVoiceRefiner:
    """SenseVoice-small 非流式复核：断句后整句重识别，补标点 + 数字规范化。

    CPU RTF ≈ 0.09（3 秒语音约 0.3 秒），同步调用可接受。
    """

    def __init__(self, model_dir: Path, num_threads: int = 2) -> None:
        import numpy as np
        import sherpa_onnx

        self._np = np
        model = _find_one(model_dir, ["*model*.int8.onnx", "*model*.onnx"])
        tokens = model_dir / "tokens.txt"
        logger.info("加载 SenseVoice 精修模型: %s", model_dir.name)
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sensevoice(
            str(model),
            str(tokens),
            num_threads=num_threads,
            use_itn=True,
        )

    def refine(self, samples: "np.ndarray") -> str:
        if samples.size == 0:
            return ""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.recognizer.decode_stream(stream)
        return str(self.recognizer.get_result(stream)).strip()
