"""Запуск анализа в отдельном процессе. Последняя строка stdout — JSON с путём и подписью."""
import json

import analyze

if __name__ == "__main__":
    path, caption, _ = analyze.build_report()
    print(json.dumps({"path": path, "caption": caption}, ensure_ascii=False))
