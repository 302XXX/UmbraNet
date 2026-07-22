"""
UmbraNet Strategy Manager

Новая модель DPI:
  • routed_domains из главного меню = единый список целей;
  • JSON-стратегия Uz1/Uz2/... = только метод обхода (args winws.exe);
  • remote_hostlists больше не используются.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("UmbraNet.StrategyManager")


class StrategyManager:
    def __init__(self, strategies_dir=None):
        if strategies_dir is None:
            current = Path(__file__).resolve().parent
            found = False
            for _ in range(6):
                candidate = current / "strategies"
                if candidate.exists():
                    self.strategies_dir = candidate.resolve()
                    found = True
                    break
                current = current.parent
            if not found:
                self.strategies_dir = (Path(__file__).resolve().parents[2] / "strategies").resolve()
        else:
            self.strategies_dir = Path(str(strategies_dir)).expanduser().resolve()
        self.strategies_dir.mkdir(parents=True, exist_ok=True)
        self.active_hostlist_path = self.strategies_dir / "active_routed_hostlist.txt"
        self.last_error = ""
        self.last_hostlist_count = 0

    @staticmethod
    def _clean_domains(lines) -> list[str]:
        out = []
        seen = set()
        for raw in lines or []:
            line = str(raw).strip().lower()
            if not line or line.startswith("#"):
                continue
            if line.startswith("||"):
                line = line[2:]
            line = line.strip("^*/ ")
            for prefix in ("https://", "http://"):
                if line.startswith(prefix):
                    line = line[len(prefix):]
            line = line.split("/")[0].strip().strip(".")
            if not line or "." not in line:
                continue
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out

    def _load_json(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Не удалось прочитать стратегию %s: %s", path.name, exc)
            return None

    def list_strategies(self, enabled_only=True):
        strategies = []
        for f in sorted(self.strategies_dir.glob("*.json")):
            data = self._load_json(f)
            if isinstance(data, dict) and "id" in data and "name" in data:
                if not enabled_only or data.get("enabled", True):
                    args = list(data.get("args", []) or []) if isinstance(data.get("args", []), list) else []
                    strategies.append({
                        "id": str(data["id"]),
                        "name": str(data["name"]),
                        "description": str(data.get("description", "")),
                        "args": args,
                        "hostlist": [],
                    })
        return strategies

    def get_strategy(self, strategy_id: str):
        sid = str(strategy_id or "").strip().lower()
        for f in self.strategies_dir.glob("*.json"):
            data = self._load_json(f)
            if isinstance(data, dict) and str(data.get("id", "")).lower() == sid:
                return data
        return None

    def _write_active_hostlist(self, routed_domains) -> tuple[str, int]:
        domains = self._clean_domains(routed_domains or [])
        self.last_hostlist_count = len(domains)
        if not domains:
            try:
                if self.active_hostlist_path.exists():
                    self.active_hostlist_path.unlink()
            except Exception:
                pass
            return "", 0
        content = "\n".join(domains) + "\n"
        try:
            old = self.active_hostlist_path.read_text(encoding="utf-8") if self.active_hostlist_path.exists() else ""
            if old != content:
                self.active_hostlist_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            self.last_error = f"Не удалось записать active hostlist: {exc}"
            log.error(self.last_error)
            return "", 0
        return f"--hostlist={self.active_hostlist_path.absolute()}", len(domains)

    def get_args(self, strategy_id: str, routed_domains=None, require_hostlist: bool = False):
        """Возвращает args для winws.exe.

        routed_domains — список целей из главного меню. Если он передан, WinWS
        ограничивается dynamic hostlist: active_routed_hostlist.txt.
        """
        self.last_error = ""
        self.last_hostlist_count = 0
        strategy = self.get_strategy(strategy_id)
        if not strategy:
            self.last_error = f"Стратегия '{strategy_id}' не найдена"
            log.error(self.last_error)
            return []

        sid = str(strategy.get("id") or strategy_id)
        raw_args = strategy.get("args", []) or []
        if not isinstance(raw_args, list):
            self.last_error = f"Стратегия '{sid}': поле args должно быть списком"
            log.error(self.last_error)
            return []
        args = [str(a).strip() for a in raw_args if str(a).strip()]
        args = [a.replace("--dpi-desync=split2,fake", "--dpi-desync=fake,split2") for a in args]
        if not args:
            self.last_error = f"Стратегия '{sid}' пока пустая. Заполните args для WinWS."
            log.warning(self.last_error)
            return []

        hostlist_arg = ""
        if routed_domains is not None:
            hostlist_arg, count = self._write_active_hostlist(routed_domains)
            if require_hostlist and not hostlist_arg:
                self.last_error = "Для DPI не выбраны цели: включите сервисы/домены в главном меню."
                log.warning(self.last_error)
                return []

        if "{hostlist}" in " ".join(args):
            if not hostlist_arg:
                self.last_error = f"Стратегия '{sid}' требует hostlist, но список целей пуст"
                log.warning(self.last_error)
                return []
            args = [arg.replace("{hostlist}", hostlist_arg) for arg in args]
        elif hostlist_arg:
            # Hostlist применяем к каждой секции winws (--new начинает новую секцию).
            new_args = [hostlist_arg]
            for arg in args:
                new_args.append(arg)
                if arg == "--new":
                    new_args.append(hostlist_arg)
            args = new_args

        bin_dir = self.strategies_dir.parent / "bin"
        lists_dir = self.strategies_dir.parent / "lists"
        final_args = []
        for arg in args:
            arg = arg.replace("{bin}", str(bin_dir.absolute()))
            arg = arg.replace("{lists}", str(lists_dir.absolute()))
            final_args.append(arg)

        unresolved = [a for a in final_args if "{" in a or "}" in a]
        if unresolved:
            self.last_error = f"Стратегия '{sid}': неразрешённые плейсхолдеры: {unresolved[:3]}"
            log.error(self.last_error)
            return []
        return final_args

    def validate_all(self) -> list[dict]:
        rows = []
        for item in self.list_strategies(enabled_only=False):
            sid = item["id"]
            args = self.get_args(sid, routed_domains=["example.com"], require_hostlist=False)
            rows.append({
                "id": sid,
                "ok": bool(args),
                "args_count": len(args),
                "error": "" if args else self.last_error,
            })
        return rows


_manager = None


def get_strategy_manager():
    global _manager
    if _manager is None:
        _manager = StrategyManager()
    return _manager
