"""应用启动六级解析：config 别名 → 开始菜单 .lnk → App Paths → PATH → UWP → 落空。

调研依据（UFO²/OpenAdapt 的启动器解析思路）：QQ NT/微信/网易云这类国产应用
不注册 App Paths 不在 PATH，但一定有开始菜单 .lnk；UWP/Store 应用走
Get-StartApps 索引 + shell:AppsFolder。解析结果缓存 7 天避免反复扫盘。
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import time
import winreg
from dataclasses import dataclass
from pathlib import Path

_CACHE_TTL_DAYS = 7
_LNK_TOP_K = 3
_MIN_SCORE = 0.34


@dataclass
class ResolvedApp:
    kind: str      # "path"（exe 直启）/ "lnk"（os.startfile 快捷方式）/ "uwp"（explorer shell:）
    target: str    # 可执行路径 / .lnk 路径 / shell:AppsFolder\<AppID>
    display: str   # 展示名


def _norm(text: str) -> str:
    return "".join(text.lower().split())


def _lnk_score(target: str, lnk_stem: str) -> float:
    """模糊分：difflib 相似度 + 子串加权。"""
    if not lnk_stem:
        return 0.0
    score = difflib.SequenceMatcher(None, target, lnk_stem).ratio()
    if target and target in lnk_stem:
        score += 0.35
    return score


class AppResolver:
    """六级解析器。powershell 只在首次扫盘/查 UWP 时调用，结果落缓存。"""

    def __init__(self, cache_path: Path, extra_apps: dict[str, str] | None = None) -> None:
        self._cache_path = cache_path
        self._extra_apps = {k.lower(): v for k, v in (extra_apps or {}).items()}
        self._cache: dict = self._load_cache()

    # ---- 缓存 ----

    def _load_cache(self) -> dict:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            age_days = (time.time() - raw.get("updated", 0)) / 86400
            if age_days <= _CACHE_TTL_DAYS:
                return raw
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {"updated": 0, "resolutions": {}, "lnk_files": [], "uwp": []}

    def _save_cache(self) -> None:
        self._cache["updated"] = time.time()
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False), encoding="utf-8"
        )

    # ---- 对外入口 ----

    def resolve(self, target: str, extra_apps: dict[str, str] | None = None) -> ResolvedApp | None:
        key = _norm(target)
        if not key:
            return None
        merged = dict(self._extra_apps)
        merged.update({k.lower(): v for k, v in (extra_apps or {}).items()})

        # 0) 历史解析缓存（应用重装场景少，命中即返）
        cached = self._cache["resolutions"].get(key)
        if cached:
            return ResolvedApp(cached["kind"], cached["target"], cached.get("display", target))

        resolved = (
            self._from_alias(target, merged)
            or self._from_lnk(key, target)
            or self._from_app_paths(key)
            or self._from_path(target)
            or self._from_uwp(key, target)
        )
        if resolved:
            self._cache["resolutions"][key] = {
                "kind": resolved.kind,
                "target": resolved.target,
                "display": resolved.display,
            }
            self._save_cache()
        return resolved

    # ---- 各级解析 ----

    def _from_alias(self, target: str, merged: dict[str, str]) -> ResolvedApp | None:
        """config 别名：值若已是路径/可执行名，直接按 path/lnk 交给后续级别精化。"""
        if target.lower() in merged:
            value = merged[target.lower()]
            if Path(value).exists():
                return ResolvedApp("path", value, target)
            exe = shutil.which(value)
            if exe:
                return ResolvedApp("path", exe, target)
            # 别名值可能是 lnk 关键字或 UWP 名——继续走 lnk/UWP 用 value 匹配
            return self._from_lnk(_norm(value), value) or self._from_uwp(_norm(value), value)
        return None

    @staticmethod
    def _program_dirs() -> list[Path]:
        dirs: list[Path] = []
        programdata = os.environ.get("ProgramData", r"C:\ProgramData")
        appdata = os.environ.get("APPDATA", "")
        dirs.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        if appdata:
            dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        return [d for d in dirs if d.exists()]

    def _collect_lnk_files(self) -> list[Path]:
        if self._cache.get("lnk_files"):
            return [Path(p) for p in self._cache["lnk_files"] if Path(p).exists()]
        found: list[Path] = []
        for base in self._program_dirs():
            found.extend(base.rglob("*.lnk"))
        self._cache["lnk_files"] = [str(p) for p in found]
        self._save_cache()
        return found

    def _from_lnk(self, key: str, target: str) -> ResolvedApp | None:
        lnks = self._collect_lnk_files()
        if not lnks:
            return None
        scored = []
        for lnk in lnks:
            stem = lnk.stem
            score = _lnk_score(key, _norm(stem))
            if score >= _MIN_SCORE:
                scored.append((score, lnk))
        scored.sort(key=lambda pair: -pair[0])
        candidates = [str(lnk) for _, lnk in scored[:_LNK_TOP_K]]
        if not candidates:
            return None
        # 一次 powershell 进程批量解析 TargetPath
        path_list = ", ".join(f"'{c.replace(chr(39), chr(39) * 2)}'" for c in candidates)
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8;"
            f"$sh = New-Object -ComObject WScript.Shell;"
            f"foreach ($p in @({path_list})) {{"
            " $s = $sh.CreateShortcut($p);"
            " if ($s -and $s.TargetPath) { Write-Output ($p + [char]9 + $s.TargetPath) } }"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, encoding="utf-8", errors="replace", timeout=25,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        targets: dict[str, str] = {}
        for line in (proc.stdout or "").splitlines():
            if "\t" not in line:
                continue
            lnk_path, target_path = line.split("\t", 1)
            targets[lnk_path.strip()] = target_path.strip()
        # 按打分序返回第一个可启动的 lnk
        for _, lnk in scored[:_LNK_TOP_K]:
            lnk_str = str(lnk)
            target_path = targets.get(lnk_str, "")
            if lnk_str in targets:  # lnk 可解析（无论目标是否存在，startfile 由 shell 裁决）
                return ResolvedApp("lnk", lnk_str, target)
        return None

    def _from_app_paths(self, key: str) -> ResolvedApp | None:
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{key}.exe"
                ) as reg_key:
                    val, _ = winreg.QueryValueEx(reg_key, "")
                    val = val.strip('"')
                    if val and Path(val).exists():
                        return ResolvedApp("path", val, key)
            except OSError:
                continue
        return None

    def _from_path(self, target: str) -> ResolvedApp | None:
        for cand in (target, f"{target}.exe"):
            found = shutil.which(cand)
            if found:
                return ResolvedApp("path", found, target)
        return None

    def _uwp_index(self) -> list[dict[str, str]]:
        if self._cache.get("uwp"):
            return self._cache["uwp"]
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8;"
                 "Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            )
            data = json.loads(proc.stdout or "[]")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            data = []
        if isinstance(data, dict):  # 单条时 ConvertTo-Json 输出对象
            data = [data]
        index = [
            {"name": str(item.get("Name", "")), "appid": str(item.get("AppID", ""))}
            for item in data
            if isinstance(item, dict) and item.get("AppID")
        ]
        self._cache["uwp"] = index
        self._save_cache()
        return index

    def _from_uwp(self, key: str, target: str) -> ResolvedApp | None:
        entries = self._uwp_index()
        if not entries:
            return None
        scored = []
        for entry in entries:
            name = _norm(entry["name"])
            score = difflib.SequenceMatcher(None, key, name).ratio()
            if key and key in name:
                score += 0.35
            if score >= 0.55:
                scored.append((score, entry))
        if not scored:
            return None
        scored.sort(key=lambda pair: -pair[0])
        appid = scored[0][1]["appid"]
        return ResolvedApp("uwp", f"shell:AppsFolder\\{appid}", target)

    # ---- 启动 ----

    def launch(self, resolved: ResolvedApp) -> None:
        if resolved.kind == "uwp":
            subprocess.Popen(["explorer.exe", resolved.target], shell=False)
        elif resolved.kind == "lnk" and hasattr(os, "startfile"):
            os.startfile(resolved.target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen([resolved.target], shell=False)
