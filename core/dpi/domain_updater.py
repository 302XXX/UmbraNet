"""
Утилита для автоматического скачивания актуальных списков доменов с GitHub.
Запускается в фоне, скачивает txt-файлы по прямым ссылкам и сохраняет в кэш.
"""
import logging
import requests
import json
from pathlib import Path

log = logging.getLogger("UmbraNet.DomainUpdater")

def update_all_strategies(strategies_dir: Path):
    for json_file in strategies_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            remote_url = data.get("remote_url")
            strat_id = data.get("id")
            
            if not remote_url or not strat_id:
                continue
                
            log.info(f"Проверка обновлений доменов для стратегии '{strat_id}'...")
            
            # Скачиваем с таймаутом, чтобы не зависать
            resp = requests.get(remote_url, timeout=10)
            resp.raise_for_status()
            
            # Очищаем от пустых строк и комментариев
            lines = [line.strip() for line in resp.text.split("\n") if line.strip() and not line.startswith("#")]
            
            if lines:
                cache_file = strategies_dir / f"remote_hostlist_{strat_id}.txt"
                old_content = ""
                if cache_file.exists():
                    old_content = cache_file.read_text(encoding="utf-8")
                    
                new_content = "\n".join(lines)
                
                # Записываем только если что-то изменилось (или файла не было)
                if new_content != old_content:
                    cache_file.write_text(new_content, encoding="utf-8")
                    log.info(f"Обновлен список доменов для '{strat_id}': загружено {len(lines)} шт.")
                else:
                    log.debug(f"Список доменов для '{strat_id}' актуален.")
                    
        except requests.RequestException as e:
            log.warning(f"Не удалось скачать домены для {json_file.name} (ошибка сети)")
        except Exception as e:
            log.warning(f"Ошибка обновления доменов для {json_file.name}: {e}")

if __name__ == "__main__":
    # Тест
    logging.basicConfig(level=logging.INFO)
    update_all_strategies(Path("../../strategies").resolve())
